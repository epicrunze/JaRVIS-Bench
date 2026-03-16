"""Orchestrates Claude Code runs on NL2Repo-Bench tasks."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from harness.config import (
    BatchResult,
    BenchConfig,
    Condition,
    RunResult,
    TaskSpec,
    discover_tasks,
    generate_batch_id,
    generate_run_id,
    load_task_spec,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Workspace setup
# ---------------------------------------------------------------------------


def setup_jarvis_workspace(workspace: Path, config: BenchConfig) -> None:
    """Install JaRVIS skills and scaffold .jarvis/ directory in the workspace."""
    # 1. Copy all skills into workspace/.claude/skills/
    skills_src = config.jarvis_dir / "skills"
    skills_dst = workspace / ".claude" / "skills"
    if skills_src.is_dir():
        shutil.copytree(skills_src, skills_dst, dirs_exist_ok=True)
    else:
        raise FileNotFoundError(f"JaRVIS skills not found: {skills_src}")

    # 2. Scaffold .jarvis/ from templates in scaffolding.md
    scaffolding_path = (
        config.jarvis_dir / "skills" / "jarvis-init" / "references" / "scaffolding.md"
    )
    _scaffold_jarvis_dir(workspace, scaffolding_path)

    # 3. Create workspace CLAUDE.md from example
    claude_md_example = (
        config.jarvis_dir / "skills" / "jarvis-init" / "references" / "CLAUDE.md.example"
    )
    if claude_md_example.exists():
        claude_md_dst = workspace / "CLAUDE.md"
        claude_md_dst.write_text(
            claude_md_example.read_text(encoding="utf-8"), encoding="utf-8"
        )


def _scaffold_jarvis_dir(workspace: Path, scaffolding_path: Path) -> None:
    """Parse scaffolding.md and create .jarvis/ files from embedded code blocks."""
    if not scaffolding_path.exists():
        raise FileNotFoundError(f"Scaffolding template not found: {scaffolding_path}")

    content = scaffolding_path.read_text(encoding="utf-8")
    jarvis_dir = workspace / ".jarvis"
    jarvis_dir.mkdir(parents=True, exist_ok=True)

    # File mapping: section header -> file path
    file_map: dict[str, Path] = {
        "IDENTITY.md": jarvis_dir / "IDENTITY.md",
        "GROWTH.md": jarvis_dir / "GROWTH.md",
        "memories/preferences.md": jarvis_dir / "memories" / "preferences.md",
        "memories/decisions.md": jarvis_dir / "memories" / "decisions.md",
    }

    # Parse sections: look for ## headings followed by code blocks
    sections = re.split(r"^## ", content, flags=re.MULTILINE)
    for section in sections:
        for filename, filepath in file_map.items():
            if section.startswith(filename):
                # Extract the first markdown code block
                match = re.search(r"```markdown\n(.*?)```", section, re.DOTALL)
                if match:
                    filepath.parent.mkdir(parents=True, exist_ok=True)
                    filepath.write_text(match.group(1), encoding="utf-8")
                break

    # Create journal directory
    (jarvis_dir / "journal").mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def build_prompt(task_spec: TaskSpec, condition: Condition) -> str:
    """Build the prompt to send to Claude Code for the given condition."""
    if condition == Condition.BASELINE:
        return _build_baseline_prompt(task_spec)
    elif condition == Condition.JARVIS_PROMPTED:
        return _build_jarvis_prompted_prompt(task_spec)
    elif condition in (Condition.ORCHESTRATED, Condition.JARVIS_ORCHESTRATED):
        raise NotImplementedError(
            f"Condition {condition.value!r} is not yet implemented."
        )
    else:
        raise ValueError(f"Unknown condition: {condition!r}")


def _build_baseline_prompt(task_spec: TaskSpec) -> str:
    return f"""You are an expert Python developer. Your task is to create a complete Python project based on the specification below.

Read the specification carefully and implement all required functionality. Create all necessary files, directories, modules, and tests. The project must be installable and all tests must pass.

Work autonomously — implement everything in a single session without asking questions.

## Project Specification

{task_spec.spec_content}"""


def _build_jarvis_prompted_prompt(task_spec: TaskSpec) -> str:
    return f"""You are an expert Python developer. Your task is to create a complete Python project based on the specification below.

**Before writing any code**, create a PLAN.md file that breaks down the implementation into logical steps.

As you work through implementation, use `/jarvis-reflect` after completing each major component to capture what you learned, what worked, and what didn't. Aim for 3-5 reflections minimum throughout the task — this helps you maintain coherence across a long implementation.

Read the specification carefully and implement all required functionality. Create all necessary files, directories, modules, and tests. The project must be installable and all tests must pass.

Work autonomously — implement everything in a single session without asking questions.

## Project Specification

{task_spec.spec_content}"""


# ---------------------------------------------------------------------------
# Claude CLI invocation
# ---------------------------------------------------------------------------


def invoke_claude(
    prompt: str, workspace: Path, config: BenchConfig
) -> tuple[subprocess.CompletedProcess[str], bool]:
    """Invoke Claude Code CLI and return (result, timed_out)."""
    cmd = [
        config.claude_command,
        "-p",
        prompt,
        "--output-format",
        config.output_format,
        "--model",
        config.model,
        "--dangerously-skip-permissions",
    ]
    if config.max_budget_usd is not None:
        cmd.extend(["--max-budget-usd", str(config.max_budget_usd)])

    logger.info("Running Claude in %s (timeout=%ds)", workspace, config.timeout_seconds)
    logger.debug("Command: %s", " ".join(cmd))

    timed_out = False
    try:
        result = subprocess.run(
            cmd,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        result = subprocess.CompletedProcess(
            args=cmd,
            returncode=-1,
            stdout=str(exc.stdout) if exc.stdout else "",
            stderr=str(exc.stderr) if exc.stderr else "",
        )

    return result, timed_out


# ---------------------------------------------------------------------------
# Workspace inspection
# ---------------------------------------------------------------------------


def list_workspace_files(workspace: Path) -> list[str]:
    """List files generated in the workspace, excluding .claude/ and .jarvis/ dirs."""
    excluded_prefixes = (".claude", ".jarvis")
    files: list[str] = []
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(workspace)
        if any(rel.parts[0] == prefix for prefix in excluded_prefixes if rel.parts):
            continue
        files.append(str(rel))
    return sorted(files)


# ---------------------------------------------------------------------------
# Run orchestration
# ---------------------------------------------------------------------------


def run_task(
    task_name: str, condition: Condition, config: BenchConfig
) -> RunResult:
    """Execute a single task under a given condition and return the result."""
    run_id = generate_run_id(task_name, condition)
    workspace = config.workspace_dir / run_id
    if workspace.exists():
        raise FileExistsError(f"Workspace already exists: {workspace}")
    workspace.mkdir(parents=True)

    task_spec = load_task_spec(task_name, config)

    # Set up JaRVIS workspace if applicable
    if condition in (Condition.JARVIS_PROMPTED, Condition.JARVIS_ORCHESTRATED):
        setup_jarvis_workspace(workspace, config)

    prompt = build_prompt(task_spec, condition)

    started_at = datetime.now(timezone.utc).isoformat()
    start_time = time.monotonic()

    result, timed_out = invoke_claude(prompt, workspace, config)

    elapsed = time.monotonic() - start_time
    finished_at = datetime.now(timezone.utc).isoformat()

    # Try to parse JSON output
    claude_output: dict[str, Any] | None = None
    if result.stdout.strip():
        try:
            claude_output = json.loads(result.stdout)
        except json.JSONDecodeError:
            logger.warning("Could not parse Claude stdout as JSON for run %s", run_id)

    files_generated = list_workspace_files(workspace)

    return RunResult(
        run_id=run_id,
        task_name=task_name,
        condition=condition,
        workspace_path=workspace,
        exit_code=result.returncode,
        wall_clock_seconds=round(elapsed, 2),
        started_at=started_at,
        finished_at=finished_at,
        timed_out=timed_out,
        raw_stdout=result.stdout,
        raw_stderr=result.stderr,
        claude_output=claude_output,
        files_generated=files_generated,
    )


def run_evaluation(
    task_name: str, condition: Condition, config: BenchConfig
) -> RunResult:
    """Run a task and persist raw outputs to results/{run_id}/raw/."""
    result = run_task(task_name, condition, config)

    # Save raw outputs
    raw_dir = config.results_dir / result.run_id / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "stdout.txt").write_text(result.raw_stdout, encoding="utf-8")
    (raw_dir / "stderr.txt").write_text(result.raw_stderr, encoding="utf-8")
    (raw_dir / "result.json").write_text(
        json.dumps(result.to_dict(), indent=2), encoding="utf-8"
    )

    return result


def run_full_benchmark(config: BenchConfig) -> BatchResult:
    """Run all task x condition x num_runs combinations sequentially."""
    batch_id = generate_batch_id()
    task_names = config.tasks if config.tasks else discover_tasks(config)
    results: list[RunResult] = []

    logger.info(
        "Starting benchmark %s: %d tasks x %d conditions x %d runs",
        batch_id,
        len(task_names),
        len(config.conditions),
        config.num_runs,
    )

    for task_name in task_names:
        for condition in config.conditions:
            for run_idx in range(config.num_runs):
                logger.info(
                    "Run %d/%d: task=%s condition=%s",
                    run_idx + 1,
                    config.num_runs,
                    task_name,
                    condition.value,
                )
                try:
                    result = run_evaluation(task_name, condition, config)
                    results.append(result)
                except Exception:
                    logger.exception(
                        "Failed: task=%s condition=%s run=%d",
                        task_name,
                        condition.value,
                        run_idx + 1,
                    )

    # Save manifest
    manifest_dir = config.results_dir / batch_id
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "manifest.json"
    manifest_data = {
        "batch_id": batch_id,
        "total_runs": len(results),
        "runs": [r.to_dict() for r in results],
    }
    manifest_path.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    return BatchResult(
        batch_id=batch_id,
        results=results,
        manifest_path=manifest_path,
    )
