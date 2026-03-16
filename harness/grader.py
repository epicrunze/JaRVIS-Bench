"""Scores evaluation results via Docker-based pytest and LLM-as-judge."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from harness.config import (
    BatchResult,
    BenchConfig,
    GradeResult,
    QualityScores,
    RunResult,
    TaskSpec,
    TaskTestData,
    TestResult,
    load_task_spec,
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

# Directories/files to skip when reading workspace for LLM judge
SKIP_DIRS = {".claude", ".jarvis", "__pycache__", ".git", ".venv", "node_modules"}

# Binary file extensions to skip
BINARY_EXTENSIONS = {
    ".pyc", ".pyo", ".so", ".o", ".a", ".dylib", ".dll",
    ".exe", ".bin", ".pkl", ".pickle", ".npy", ".npz",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".whl", ".egg", ".db", ".sqlite", ".sqlite3",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
}


# ---------------------------------------------------------------------------
# Docker-based pytest grading
# ---------------------------------------------------------------------------


def _load_test_data(task_name: str, config: BenchConfig) -> TaskTestData:
    """Load test configuration for a task."""
    return load_task_test_data(task_name, config)


def _stage_workspace(
    workspace_path: Path, test_data: TaskTestData
) -> Path:
    """Copy workspace to a temp dir, removing package and test files."""
    staging_dir = Path(tempfile.mkdtemp(prefix=f"jarvis-bench-stage-{test_data.task_name}-"))
    workspace_copy = staging_dir / "workspace"
    shutil.copytree(workspace_path, workspace_copy, dirs_exist_ok=True)

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
FROM ghcr.io/multimodal-art-projection/nl2repobench/{task_name}:1.0

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
    image_tag = f"jarvis-bench-test-{run_id}"

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
    container_name = f"jarvis-bench-{run_id}"
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

        # Execute each test command
        for command in test_commands:
            logger.info("Executing: %s", command)
            result = subprocess.run(
                [
                    "docker", "exec", container_name,
                    "sh", "-c", command,
                ],
                capture_output=True,
                text=True,
                timeout=600,
            )
            command_results.append({
                "command": command,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            })
    finally:
        _cleanup_docker(run_id)

    return command_results


def _cleanup_docker(run_id: str) -> None:
    """Remove container and image for a run."""
    container_name = f"jarvis-bench-{run_id}"
    image_tag = f"jarvis-bench-test-{run_id}"

    for cmd in [
        ["docker", "rm", "-f", container_name],
        ["docker", "rmi", image_tag],
    ]:
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except Exception:
            logger.warning("Cleanup command failed: %s", " ".join(cmd))


def _parse_pytest_output(
    command_results: list[dict[str, Any]], total_test_cases: int
) -> TestResult:
    """Parse pytest output from command results."""
    passed = 0
    failed = 0
    errors = 0

    for result in command_results:
        command = result["command"]
        if "pytest" not in command.lower():
            continue

        output = result.get("stdout", "") + "\n" + result.get("stderr", "")
        for line in output.split("\n"):
            m = re.search(r"(\d+) passed", line)
            if m:
                passed += int(m.group(1))
            m = re.search(r"(\d+) failed", line)
            if m:
                failed += int(m.group(1))
            m = re.search(r"(\d+) error", line)
            if m:
                errors += int(m.group(1))

    total = total_test_cases if total_test_cases > 0 else (passed + failed + errors)
    success_rate = min(passed / total, 1.0) if total > 0 else 0.0

    return TestResult(
        passed=passed,
        failed=failed,
        errors=errors,
        total=total,
        success_rate=success_rate,
        command_outputs=command_results,
    )


def grade_with_docker(run_result: RunResult, config: BenchConfig) -> TestResult:
    """Grade a completed workspace using Docker-based pytest."""
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
# LLM-as-judge grading
# ---------------------------------------------------------------------------


def _read_workspace_files(
    workspace_path: Path, max_file_size: int = 100_000
) -> dict[str, str]:
    """Read text files from workspace, skipping binary and internal dirs."""
    files: dict[str, str] = {}

    for path in sorted(workspace_path.rglob("*")):
        if not path.is_file():
            continue

        rel = path.relative_to(workspace_path)
        # Skip internal directories
        if rel.parts and rel.parts[0] in SKIP_DIRS:
            continue
        # Skip binary extensions
        if path.suffix.lower() in BINARY_EXTENSIONS:
            continue

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            if len(content) > max_file_size:
                content = content[:max_file_size] + "\n... [truncated]"
            files[str(rel)] = content
        except Exception:
            logger.debug("Could not read file: %s", path)

    return files


def _build_judge_prompt(
    spec_content: str, file_tree: list[str], file_contents: dict[str, str]
) -> tuple[str, str]:
    """Build system and user prompts for the LLM judge."""
    system_prompt = (
        "You are a code quality evaluator. You assess Python projects against "
        "their specifications on three dimensions. Be fair but rigorous. "
        "Return your evaluation as a JSON object with exactly these keys: "
        "architectural_coherence (0-10), code_quality (0-10), completeness (0-10), "
        'rationale (string). No markdown fencing, just raw JSON.'
    )

    files_section = "\n".join(f"  {f}" for f in file_tree)

    contents_section = ""
    for path, content in file_contents.items():
        contents_section += f"\n### {path}\n```\n{content}\n```\n"

    user_prompt = f"""\
## Original Specification

{spec_content}

## Generated File Tree

{files_section}

## File Contents
{contents_section}

## Evaluation Rubric

- **architectural_coherence** (0-10): How well-organized is the code? Are modules logically separated? Is there a clear structure that matches the spec's requirements?
- **code_quality** (0-10): Is the code clean, idiomatic Python? Proper error handling, naming, typing?
- **completeness** (0-10): How much of the specification is actually implemented? Are all required features present?

Rate each dimension 0-10 and provide a brief rationale explaining your scores.
Return ONLY a JSON object with keys: architectural_coherence, code_quality, completeness, rationale."""

    return system_prompt, user_prompt


def grade_with_llm(
    run_result: RunResult, task_spec: TaskSpec, config: BenchConfig
) -> QualityScores:
    """Grade workspace quality using LLM-as-judge."""
    import anthropic

    workspace_files = _read_workspace_files(run_result.workspace_path)
    file_tree = sorted(workspace_files.keys())

    system_prompt, user_prompt = _build_judge_prompt(
        task_spec.spec_content, file_tree, workspace_files
    )

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    # Extract text from response
    first_block = response.content[0]
    if not hasattr(first_block, "text"):
        raise ValueError(f"Unexpected response block type: {type(first_block)}")
    response_text: str = first_block.text  # type: ignore[union-attr]

    # Parse JSON — handle possible markdown fencing
    json_text = response_text.strip()
    if json_text.startswith("```"):
        # Strip markdown code fences
        json_text = re.sub(r"^```\w*\n?", "", json_text)
        json_text = re.sub(r"\n?```$", "", json_text)

    scores = json.loads(json_text)

    arch = float(scores["architectural_coherence"])
    quality = float(scores["code_quality"])
    completeness = float(scores["completeness"])
    overall = round((arch + quality + completeness) / 3, 2)

    return QualityScores(
        architectural_coherence=arch,
        code_quality=quality,
        completeness=completeness,
        overall=overall,
        rationale=scores.get("rationale", ""),
    )


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


def grade_run(run_result: RunResult, config: BenchConfig) -> GradeResult:
    """Grade a single run with both Docker pytest and LLM judge."""
    test_result: TestResult | None = None
    quality_scores: QualityScores | None = None

    # 1. Docker pytest grading
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

    # 2. LLM judge grading
    try:
        task_spec = load_task_spec(run_result.task_name, config)
        quality_scores = grade_with_llm(run_result, task_spec, config)
        logger.info(
            "LLM grading complete for %s: overall=%.1f",
            run_result.run_id,
            quality_scores.overall,
        )
    except Exception:
        logger.exception("LLM grading failed for %s", run_result.run_id)

    # 3. Save results
    results_dir = config.results_dir / run_result.run_id
    results_dir.mkdir(parents=True, exist_ok=True)

    if test_result is not None:
        (results_dir / "test_results.json").write_text(
            json.dumps(test_result.to_dict(), indent=2), encoding="utf-8"
        )

    if quality_scores is not None:
        (results_dir / "quality_scores.json").write_text(
            json.dumps(quality_scores.to_dict(), indent=2), encoding="utf-8"
        )

    grade = GradeResult(
        run_id=run_result.run_id,
        task_name=run_result.task_name,
        condition=run_result.condition,
        test_result=test_result,
        quality_scores=quality_scores,
    )

    (results_dir / "grades.json").write_text(
        json.dumps(grade.to_dict(), indent=2), encoding="utf-8"
    )

    return grade


def grade_batch(
    batch_result: BatchResult, config: BenchConfig
) -> list[GradeResult]:
    """Grade all runs in a batch."""
    grades: list[GradeResult] = []
    for run_result in batch_result.results:
        grade = grade_run(run_result, config)
        grades.append(grade)
    return grades
