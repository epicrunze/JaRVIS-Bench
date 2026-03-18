"""Docker abstraction layer for running Claude Code in isolated containers."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.config import BenchConfig

logger = logging.getLogger(__name__)

# Directories to exclude from mtime scanning — these are touched by
# Claude CLI internals, not by actual agent file-writing work.
_MTIME_EXCLUDE_DIRS = {".claude-home", ".git"}


def _get_latest_mtime(workspace: Path) -> float:
    """Return the most recent st_mtime across all files in *workspace*.

    Excludes directories listed in ``_MTIME_EXCLUDE_DIRS`` and uses
    ``os.scandir`` recursively for speed on large workspaces.
    """

    latest = 0.0

    def _scan(directory: Path) -> None:
        nonlocal latest
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in _MTIME_EXCLUDE_DIRS:
                            _scan(Path(entry.path))
                    else:
                        try:
                            mtime = entry.stat(follow_symlinks=False).st_mtime
                            if mtime > latest:
                                latest = mtime
                        except OSError:
                            pass
        except OSError:
            pass

    _scan(workspace)
    return latest


def check_docker_available() -> bool:
    """Check if the Docker daemon is reachable."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def ensure_runner_image(dockerfile_dir: Path, tag: str) -> str:
    """Ensure the runner Docker image exists, building it if missing. Returns tag."""
    # Check if image already exists
    result = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0:
        logger.info("Runner image %s already exists", tag)
        return tag

    # Build from Dockerfile.runner
    dockerfile_path = dockerfile_dir / "Dockerfile.runner"
    if not dockerfile_path.exists():
        raise FileNotFoundError(f"Dockerfile.runner not found: {dockerfile_path}")

    logger.info("Building runner image %s from %s", tag, dockerfile_path)
    result = subprocess.run(
        ["docker", "build", "-f", str(dockerfile_path), "-t", tag, str(dockerfile_dir)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to build runner image {tag}: {result.stderr}")

    return tag


def run_claude_in_container(
    prompt: str,
    workspace: Path,
    config: BenchConfig,
    run_id: str,
) -> tuple[str, str, int, bool]:
    """Run Claude Code inside a Docker container.

    Returns (stdout, stderr, exit_code, timed_out).
    """
    # Ensure image exists
    dockerfile_dir = config.project_root / "docker"
    ensure_runner_image(dockerfile_dir, config.docker_image)

    container_name = f"jarvis-run-{run_id}"
    abs_workspace = str(workspace.resolve())
    claude_home = str(Path.home() / ".claude")
    claude_json = Path.home() / ".claude.json"

    # Build claude command args
    claude_args = [
        "-p", prompt,
        "--output-format", config.output_format,
        "--model", config.model,
        "--dangerously-skip-permissions",
    ]
    if config.max_budget_usd is not None:
        claude_args.extend(["--max-budget-usd", str(config.max_budget_usd)])
    if config.max_turns is not None:
        claude_args.extend(["--max-turns", str(config.max_turns)])

    # Start container in detached mode
    # Use host UID/GID so mounted auth files are readable
    host_uid = os.getuid()
    host_gid = os.getgid()

    # Build a minimal writable ~/.claude for the container.
    # Claude CLI needs: settings.json (config), session-env/ (Bash tool),
    # and ~/.claude.json (auth + Skill tool writes to it).
    # We do NOT copy plugins/, projects/, telemetry/, etc. (~350MB of junk).
    workspace_claude_dir = workspace / ".claude-home"
    workspace_claude_dir.mkdir(parents=True, exist_ok=True)
    claude_home_path = Path(claude_home)
    for fname in ("settings.json", ".credentials.json"):
        src = claude_home_path / fname
        if src.exists():
            shutil.copy2(src, workspace_claude_dir / fname)
    (workspace_claude_dir / "session-env").mkdir(exist_ok=True)
    workspace_claude_json = workspace / ".claude.json"
    if claude_json.exists():
        shutil.copy2(claude_json, workspace_claude_json)

    volume_mounts = [
        "-v", f"{abs_workspace}:/workspace:rw",
        "-v", f"{workspace_claude_dir.resolve()}:/home/pn/.claude:rw",
        "-v", f"{workspace_claude_json.resolve()}:/home/pn/.claude.json:rw",
    ]

    run_cmd = [
        "docker", "run", "-d",
        "--name", container_name,
        "--user", f"{host_uid}:{host_gid}",
        "-e", "HOME=/home/pn",
        "-e", "JARVIS_DIR=/workspace/.jarvis",
        *volume_mounts,
        "-w", "/workspace",
        f"--memory={config.docker_memory}",
        f"--cpus={config.docker_cpus}",
        config.docker_image,
        *claude_args,
    ]

    logger.info("Starting container %s", container_name)
    logger.debug("Docker run command: %s", " ".join(run_cmd))

    try:
        start_result = subprocess.run(
            run_cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if start_result.returncode != 0:
            raise RuntimeError(
                f"Failed to start container {container_name}: {start_result.stderr}"
            )

        # Poll for container exit or idle timeout
        timed_out = False
        idle_timed_out = False
        exit_code = -1
        start_time = time.monotonic()
        # Track mtime (epoch-based) and monotonic time separately
        last_known_mtime = _get_latest_mtime(workspace)
        last_activity_mono = start_time

        while True:
            elapsed = time.monotonic() - start_time

            # Check overall timeout
            if elapsed >= config.timeout_seconds:
                timed_out = True
                logger.warning(
                    "Container %s hit hard timeout after %ds, killing",
                    container_name, int(elapsed),
                )
                break

            # Check if container has exited
            try:
                inspect = subprocess.run(
                    ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
                    capture_output=True, text=True, timeout=5,
                )
                if inspect.stdout.strip() == "false":
                    wait_result = subprocess.run(
                        ["docker", "wait", container_name],
                        capture_output=True, text=True, timeout=5,
                    )
                    exit_code = int(wait_result.stdout.strip()) if wait_result.stdout.strip() else -1
                    break
            except (subprocess.TimeoutExpired, ValueError):
                pass

            # Check workspace activity — compare mtimes to detect new writes,
            # but track idle duration on the monotonic clock
            latest_mtime = _get_latest_mtime(workspace)
            if latest_mtime > last_known_mtime:
                last_known_mtime = latest_mtime
                last_activity_mono = time.monotonic()

            idle_seconds = time.monotonic() - last_activity_mono
            if idle_seconds >= config.idle_timeout_seconds:
                idle_timed_out = True
                timed_out = True
                logger.warning(
                    "Container %s idle for %ds (no file changes), killing",
                    container_name, int(idle_seconds),
                )
                break

            time.sleep(10)

        # Capture logs BEFORE killing — killed containers may lose buffered output
        logs_result = subprocess.run(
            ["docker", "logs", container_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        stdout = logs_result.stdout
        stderr = logs_result.stderr

        # Now kill if needed
        if timed_out:
            timeout_kind = "idle" if idle_timed_out else "hard"
            logger.info("Killing container %s (%s timeout)", container_name, timeout_kind)
            subprocess.run(
                ["docker", "kill", container_name],
                capture_output=True,
                text=True,
                timeout=10,
            )

        return stdout, stderr, exit_code, timed_out

    finally:
        # Remove copied auth files from workspace
        if workspace_claude_json.exists():
            workspace_claude_json.unlink(missing_ok=True)
        if workspace_claude_dir.exists():
            shutil.rmtree(workspace_claude_dir, ignore_errors=True)
        # Cleanup container
        for cmd in [
            ["docker", "rm", "-f", container_name],
        ]:
            try:
                subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            except Exception:
                logger.warning("Cleanup failed: %s", " ".join(cmd))
