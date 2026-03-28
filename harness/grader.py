"""Scores evaluation results via Docker-based pytest and LLM-as-judge."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from harness.config import (
    BatchResult,
    BenchConfig,
    GradeResult,
    RunResult,
    TaskTestData,
    TestResult,
    load_task_test_data,
)

logger = logging.getLogger(__name__)

# Package files to remove before testing (matches NL2RepoBench post_processor.py)
PACKAGE_FILES = {
    "setup.py",
    "pyproject.toml",
    "setup.cfg",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
    "tox.ini",
    "pytest.ini",
    "poetry.lock",
    "Pipfile",
    "Pipfile.lock",
    "environment.yml",
    "conda-env.yaml",
    "manifest.in",
    "MANIFEST.in",
}


# ---------------------------------------------------------------------------
# Docker-based pytest grading
# ---------------------------------------------------------------------------


def _load_test_data(task_name: str, config: BenchConfig) -> TaskTestData:
    """Load test configuration for a task."""
    return load_task_test_data(task_name, config)


_STAGE_SKIP_DIRS = {".venv", "__pycache__", ".pytest_cache", ".git", ".jarvis", ".claude", "node_modules"}


def _stage_workspace(
    workspace_path: Path, test_data: TaskTestData
) -> Path:
    """Copy workspace to a temp dir, removing package and test files."""
    staging_dir = Path(tempfile.mkdtemp(prefix=f"jarvis-bench-stage-{test_data.task_name}-"))
    workspace_copy = staging_dir / "workspace"
    shutil.copytree(
        workspace_path,
        workspace_copy,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(*_STAGE_SKIP_DIRS),
    )

    # Remove package management files (walk all dirs, matching post_processor.py)
    for root, _dirs, files in os.walk(workspace_copy):
        for filename in files:
            if filename in PACKAGE_FILES:
                filepath = Path(root) / filename
                filepath.unlink()
                logger.debug("Removed package file: %s", filepath)

    # Remove test files/dirs listed in test_files.json
    for test_file in test_data.test_files:
        target = workspace_copy / test_file
        if target.is_dir():
            shutil.rmtree(target)
            logger.debug("Removed test directory: %s", target)
        elif target.is_file():
            target.unlink()
            logger.debug("Removed test file: %s", target)

    return staging_dir


def _write_dockerfile(staging_dir: Path, task_name: str) -> Path:
    """Write a Dockerfile into the staging directory."""
    dockerfile_path = staging_dir / "Dockerfile"
    dockerfile_content = f"""\
FROM ghcr.io/multimodal-art-projection/nl2repobench/{task_name.lower()}:1.0

COPY workspace /workspace

WORKDIR /workspace

ENV PYTHONPATH=/workspace:$PYTHONPATH

CMD ["tail", "-f", "/dev/null"]
"""
    dockerfile_path.write_text(dockerfile_content, encoding="utf-8")
    return dockerfile_path


def _build_test_image(staging_dir: Path, task_name: str, run_id: str) -> str:
    """Build a Docker image for testing. Returns the image tag."""
    _write_dockerfile(staging_dir, task_name)
    image_tag = f"jarvis-bench-test-{run_id}".lower()

    logger.info("Building Docker image %s", image_tag)
    result = subprocess.run(
        ["docker", "build", "-t", image_tag, "."],
        cwd=staging_dir,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        logger.error("Docker build failed:\n%s", result.stderr)
        raise RuntimeError(f"Docker build failed for {image_tag}: {result.stderr}")

    return image_tag


def _run_tests_in_container(
    image_tag: str, test_commands: list[str], run_id: str
) -> list[dict[str, Any]]:
    """Run test commands inside a Docker container."""
    container_name = f"jarvis-bench-{run_id}".lower()
    command_results: list[dict[str, Any]] = []

    try:
        # Start container
        subprocess.run(
            [
                "docker", "run", "-d",
                "--name", container_name,
                image_tag,
                "tail", "-f", "/dev/null",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        logger.info("Started test container %s", container_name)

        # Install pytest-timeout to handle hanging individual tests
        logger.info("Installing pytest-timeout in container")
        subprocess.run(
            [
                "docker", "exec", container_name,
                "pip", "install", "pytest-timeout",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        # Execute each test command
        for command in test_commands:
            # Inject per-test timeout for pytest commands
            if "pytest" in command.lower() and "--timeout" not in command:
                command = command + " --timeout=120"

            logger.info("Executing: %s", command)
            try:
                result = subprocess.run(
                    [
                        "docker", "exec", container_name,
                        "sh", "-c", command,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=1800,
                )
                command_results.append({
                    "command": command,
                    "exit_code": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                })
            except subprocess.TimeoutExpired as exc:
                logger.warning("Command timed out after 1800s: %s", command)
                command_results.append({
                    "command": command,
                    "exit_code": -1,
                    "stdout": str(exc.stdout) if exc.stdout else "",
                    "stderr": str(exc.stderr) if exc.stderr else "",
                    "timed_out": True,
                })
    finally:
        _cleanup_docker(run_id)

    return command_results


def _cleanup_docker(run_id: str) -> None:
    """Remove container and image for a run."""
    container_name = f"jarvis-bench-{run_id}".lower()
    image_tag = f"jarvis-bench-test-{run_id}".lower()

    for cmd in [
        ["docker", "rm", "-f", container_name],
        ["docker", "rmi", image_tag],
    ]:
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except Exception:
            logger.warning("Cleanup command failed: %s", " ".join(cmd))


def _parse_summary_line(line: str) -> dict[str, int]:
    """Parse pytest summary line like '=== 5 passed, 2 failed, 1 error in 3.2s ==='."""
    counts: dict[str, int] = {}
    # Match patterns like "5 passed", "2 failed", "1 error", "3 skipped", etc.
    for m in re.finditer(r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed|warnings?|deselected)", line):
        key = m.group(2)
        # Normalize: "errors" → "error", "warnings" → "warning"
        if key == "errors":
            key = "error"
        if key == "warnings":
            key = "warning"
        counts[key] = counts.get(key, 0) + int(m.group(1))
    return counts


def _parse_collected_count(output: str) -> int:
    """Parse 'collected N items' from pytest output. Returns 0 if not found."""
    # Match "collected N items" or "collected N items / M errors"
    m = re.search(r"collected (\d+) items?", output)
    return int(m.group(1)) if m else 0


def _parse_pytest_output(
    command_results: list[dict[str, Any]], total_test_cases: int
) -> TestResult:
    """Parse pytest output from command results.

    Parses only the LAST pytest summary line (=== ... ===) per command to avoid
    double-counting from subprocess pytest invocations (e.g., pytest-cov tests).
    Uses the actual 'collected N items' count as total when available.
    """
    passed = 0
    failed = 0
    errors = 0
    skipped = 0
    xfailed = 0
    xpassed = 0
    warnings = 0
    collected = 0
    pip_install_failed = False
    command_timed_out = False

    for result in command_results:
        command = result.get("command", "")

        # Track pip install failures
        if "pytest" not in command.lower():
            if result.get("exit_code", 0) != 0 and "pip" in command.lower():
                pip_install_failed = True
            continue

        # Track command timeouts
        if result.get("timed_out"):
            command_timed_out = True

        output = result.get("stdout", "") + "\n" + result.get("stderr", "")

        # Parse collected count from this command
        cmd_collected = _parse_collected_count(output)
        collected += cmd_collected

        # Find the LAST summary line (=== ... ===) to avoid intermediate counts
        summary_lines = []
        for line in output.split("\n"):
            # Pytest summary lines are bordered with '=' chars
            stripped = line.strip()
            if re.match(r"^=+\s.*\s=+$", stripped) and re.search(
                r"(passed|failed|error|no tests ran)", stripped
            ):
                summary_lines.append(stripped)

        if summary_lines:
            # Use only the last summary line
            counts = _parse_summary_line(summary_lines[-1])
            passed += counts.get("passed", 0)
            failed += counts.get("failed", 0)
            errors += counts.get("error", 0)
            skipped += counts.get("skipped", 0)
            xfailed += counts.get("xfailed", 0)
            xpassed += counts.get("xpassed", 0)
            warnings += counts.get("warning", 0)

    # Use collected count as total when available, fall back to test_case_count.txt
    if collected > 0:
        total = collected
    elif total_test_cases > 0:
        total = total_test_cases
    else:
        total = passed + failed + errors

    success_rate = min(passed / total, 1.0) if total > 0 else 0.0

    return TestResult(
        passed=passed,
        failed=failed,
        errors=errors,
        total=total,
        success_rate=success_rate,
        command_outputs=command_results,
        skipped=skipped,
        xfailed=xfailed,
        xpassed=xpassed,
        warnings=warnings,
        collected=collected,
        expected_total=total_test_cases,
        pip_install_failed=pip_install_failed,
        command_timed_out=command_timed_out,
    )


def _ensure_base_image(task_name: str) -> None:
    """Ensure the NL2RepoBench base image exists, pulling if needed."""
    image = f"ghcr.io/multimodal-art-projection/nl2repobench/{task_name.lower()}:1.0"
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0:
        return

    logger.info("Pulling base image %s", image)
    result = subprocess.run(
        ["docker", "pull", image],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to pull base image {image}: {result.stderr}"
        )


def grade_with_docker(run_result: RunResult, config: BenchConfig) -> TestResult:
    """Grade a completed workspace using Docker-based pytest."""
    _ensure_base_image(run_result.task_name)
    test_data = _load_test_data(run_result.task_name, config)
    staging_dir = _stage_workspace(run_result.workspace_path, test_data)

    try:
        image_tag = _build_test_image(staging_dir, run_result.task_name, run_result.run_id)
        command_results = _run_tests_in_container(
            image_tag, test_data.test_commands, run_result.run_id
        )
        return _parse_pytest_output(command_results, test_data.test_case_count)
    finally:
        # Clean up staging directory
        shutil.rmtree(staging_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


def _load_existing_grade(grades_path: Path, run_result: RunResult) -> GradeResult:
    """Load a previously saved GradeResult from disk."""
    data = json.loads(grades_path.read_text(encoding="utf-8"))
    tr = data.get("test_result")
    loaded_result = TestResult(
        passed=tr.get("passed", 0),
        failed=tr.get("failed", 0),
        errors=tr.get("errors", 0),
        total=tr.get("total", 0),
        success_rate=tr.get("success_rate", 0.0),
        command_outputs=tr.get("command_outputs", []),
        skipped=tr.get("skipped", 0),
        xfailed=tr.get("xfailed", 0),
        xpassed=tr.get("xpassed", 0),
        warnings=tr.get("warnings", 0),
        collected=tr.get("collected", 0),
        expected_total=tr.get("expected_total", 0),
        pip_install_failed=tr.get("pip_install_failed", False),
        command_timed_out=tr.get("command_timed_out", False),
    ) if tr else None
    return GradeResult(
        run_id=run_result.run_id,
        task_name=run_result.task_name,
        condition=run_result.condition,
        test_result=loaded_result,
        quality_scores=None,
    )


def grade_run(run_result: RunResult, config: BenchConfig, *, force: bool = False) -> GradeResult:
    """Grade a single run with Docker pytest."""
    results_dir = config.results_dir / run_result.run_id
    grades_path = results_dir / "grades.json"

    # Skip if already graded (unless --force)
    if not force and grades_path.exists():
        logger.info("Skipping %s (already graded, use --force to re-grade)", run_result.run_id)
        return _load_existing_grade(grades_path, run_result)

    test_result: TestResult | None = None

    # Skip grading for timed-out or errored runs
    skip_reason: str | None = None
    if run_result.timed_out:
        skip_reason = f"Skipped: run timed out ({run_result.run_id})"
    elif run_result.error:
        skip_reason = f"Skipped: run failed with error: {run_result.error}"

    if skip_reason:
        logger.warning("%s", skip_reason)
        test_result = TestResult(
            passed=0,
            failed=0,
            errors=0,
            total=0,
            success_rate=0.0,
            command_outputs=[{"note": skip_reason}],
        )
        results_dir.mkdir(parents=True, exist_ok=True)
        (results_dir / "test_results.json").write_text(
            json.dumps(test_result.to_dict(), indent=2), encoding="utf-8"
        )
        grade = GradeResult(
            run_id=run_result.run_id,
            task_name=run_result.task_name,
            condition=run_result.condition,
            test_result=test_result,
            quality_scores=None,
        )
        (results_dir / "grades.json").write_text(
            json.dumps(grade.to_dict(), indent=2), encoding="utf-8"
        )
        return grade

    # Docker pytest grading
    try:
        test_result = grade_with_docker(run_result, config)
        logger.info(
            "Docker grading complete for %s: %d/%d passed (%.1f%%)",
            run_result.run_id,
            test_result.passed,
            test_result.total,
            test_result.success_rate * 100,
        )
    except Exception:
        logger.exception("Docker grading failed for %s", run_result.run_id)

    # Save results
    results_dir.mkdir(parents=True, exist_ok=True)

    if test_result is not None:
        (results_dir / "test_results.json").write_text(
            json.dumps(test_result.to_dict(), indent=2), encoding="utf-8"
        )

    grade = GradeResult(
        run_id=run_result.run_id,
        task_name=run_result.task_name,
        condition=run_result.condition,
        test_result=test_result,
        quality_scores=None,
    )

    (results_dir / "grades.json").write_text(
        json.dumps(grade.to_dict(), indent=2), encoding="utf-8"
    )

    return grade


def grade_batch(
    batch_result: BatchResult, config: BenchConfig, *, force: bool = False
) -> list[GradeResult]:
    """Grade all runs in a batch.

    When config.max_workers > 1, runs are graded concurrently using threads.
    """
    total = len(batch_result.results)

    if config.max_workers <= 1:
        grades: list[GradeResult] = []
        for run_result in batch_result.results:
            grade = grade_run(run_result, config, force=force)
            grades.append(grade)
        return grades

    grades = []
    completed = 0
    with ThreadPoolExecutor(max_workers=config.max_workers) as executor:
        futures = {
            executor.submit(grade_run, run_result, config, force=force): run_result
            for run_result in batch_result.results
        }
        for future in as_completed(futures):
            run_result = futures[future]
            try:
                grade = future.result()
                grades.append(grade)
            except Exception:
                logger.exception("Grading failed for %s", run_result.run_id)
            completed += 1
            logger.info("Graded %d/%d runs", completed, total)
    return grades
