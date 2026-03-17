"""Docker abstraction layer for running Claude Code in isolated containers."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.config import BenchConfig

logger = logging.getLogger(__name__)


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
    import os
    host_uid = os.getuid()
    host_gid = os.getgid()

    volume_mounts = [
        "-v", f"{abs_workspace}:/workspace:rw",
        "-v", f"{claude_home}:/home/claude/.claude:ro",
    ]
    # Mount ~/.claude.json for auth if it exists
    if claude_json.exists():
        volume_mounts.extend(["-v", f"{claude_json}:/home/claude/.claude.json:ro"])

    run_cmd = [
        "docker", "run", "-d",
        "--name", container_name,
        "--user", f"{host_uid}:{host_gid}",
        "-e", "HOME=/home/claude",
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

        # Wait for container to finish (with timeout)
        timed_out = False
        try:
            wait_result = subprocess.run(
                ["docker", "wait", container_name],
                capture_output=True,
                text=True,
                timeout=config.timeout_seconds,
            )
            exit_code = int(wait_result.stdout.strip()) if wait_result.stdout.strip() else -1
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = -1
            logger.warning("Container %s timed out after %ds, killing", container_name, config.timeout_seconds)
            subprocess.run(
                ["docker", "kill", container_name],
                capture_output=True,
                text=True,
                timeout=10,
            )

        # Capture logs
        logs_result = subprocess.run(
            ["docker", "logs", container_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        stdout = logs_result.stdout
        stderr = logs_result.stderr

        return stdout, stderr, exit_code, timed_out

    finally:
        # Cleanup container
        for cmd in [
            ["docker", "rm", "-f", container_name],
        ]:
            try:
                subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            except Exception:
                logger.warning("Cleanup failed: %s", " ".join(cmd))
