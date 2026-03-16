"""Configuration and defaults for JaRVIS-Bench evaluation runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class Condition(str, Enum):
    """Experimental conditions for evaluation runs."""

    BASELINE = "baseline"
    JARVIS_PROMPTED = "jarvis-prompted"
    ORCHESTRATED = "orchestrated"
    JARVIS_ORCHESTRATED = "jarvis-orchestrated"


@dataclass(frozen=True)
class TaskSpec:
    """A single NL2Repo-Bench task specification."""

    name: str
    spec_path: Path
    test_commands_path: Path
    spec_content: str

    @classmethod
    def from_task_dir(cls, task_name: str, task_dir: Path) -> TaskSpec:
        spec_path = task_dir / "start.md"
        test_commands_path = task_dir / "test_commands.json"

        if not spec_path.exists():
            raise FileNotFoundError(f"Task spec not found: {spec_path}")
        if not test_commands_path.exists():
            raise FileNotFoundError(f"Test commands not found: {test_commands_path}")

        spec_content = spec_path.read_text(encoding="utf-8")
        return cls(
            name=task_name,
            spec_path=spec_path,
            test_commands_path=test_commands_path,
            spec_content=spec_content,
        )


@dataclass
class RunResult:
    """Result of a single Claude Code evaluation run."""

    run_id: str
    task_name: str
    condition: Condition
    workspace_path: Path

    # Execution
    exit_code: int
    wall_clock_seconds: float
    started_at: str
    finished_at: str
    timed_out: bool

    # Claude output
    raw_stdout: str
    raw_stderr: str
    claude_output: dict[str, Any] | None

    # Workspace
    files_generated: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_name": self.task_name,
            "condition": self.condition.value,
            "workspace_path": str(self.workspace_path),
            "exit_code": self.exit_code,
            "wall_clock_seconds": self.wall_clock_seconds,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "timed_out": self.timed_out,
            "raw_stdout": self.raw_stdout,
            "raw_stderr": self.raw_stderr,
            "claude_output": self.claude_output,
            "files_generated": self.files_generated,
        }


@dataclass
class TestResult:
    """Docker-based pytest evaluation result."""

    passed: int
    failed: int
    errors: int
    total: int
    success_rate: float
    command_outputs: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "total": self.total,
            "success_rate": self.success_rate,
            "command_outputs": self.command_outputs,
        }


@dataclass
class QualityScores:
    """LLM-as-judge quality evaluation."""

    architectural_coherence: float  # 0-10
    code_quality: float  # 0-10
    completeness: float  # 0-10
    overall: float  # average
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "architectural_coherence": self.architectural_coherence,
            "code_quality": self.code_quality,
            "completeness": self.completeness,
            "overall": self.overall,
            "rationale": self.rationale,
        }


@dataclass
class GradeResult:
    """Combined grading result for a single run."""

    run_id: str
    task_name: str
    condition: Condition
    test_result: TestResult | None
    quality_scores: QualityScores | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_name": self.task_name,
            "condition": self.condition.value,
            "test_result": self.test_result.to_dict() if self.test_result else None,
            "quality_scores": (
                self.quality_scores.to_dict() if self.quality_scores else None
            ),
        }


@dataclass
class BatchResult:
    """Aggregated results from a full benchmark run."""

    batch_id: str
    results: list[RunResult]
    manifest_path: Path


@dataclass(frozen=True)
class TaskTestData:
    """Test configuration for a task (from NL2RepoBench)."""

    task_name: str
    test_commands: list[str]
    test_files: list[str]
    test_case_count: int


@dataclass
class BenchConfig:
    """Top-level configuration for a benchmark evaluation."""

    project_root: Path

    # Claude settings
    claude_command: str = "claude"
    model: str = "claude-sonnet-4-20250514"
    timeout_seconds: int = 1200
    max_budget_usd: float | None = None
    output_format: str = "json"

    # Eval settings
    conditions: list[Condition] = field(
        default_factory=lambda: [Condition.BASELINE, Condition.JARVIS_PROMPTED]
    )
    num_runs: int = 3
    tasks: list[str] | None = None

    # Derived paths — set in __post_init__
    vendor_dir: Path = field(init=False)
    workspace_dir: Path = field(init=False)
    results_dir: Path = field(init=False)
    nl2repo_dir: Path = field(init=False)
    jarvis_dir: Path = field(init=False)
    test_files_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self.project_root = self.project_root.resolve()
        self.vendor_dir = self.project_root / "vendor"
        self.workspace_dir = self.project_root / "workspaces"
        self.results_dir = self.project_root / "results"
        self.nl2repo_dir = self.vendor_dir / "NL2RepoBench"
        self.jarvis_dir = self.vendor_dir / "JaRVIS"
        self.test_files_dir = self.nl2repo_dir / "test_files"


def load_task_spec(task_name: str, config: BenchConfig) -> TaskSpec:
    """Load a task specification by name."""
    task_dir = config.test_files_dir / task_name
    if not task_dir.is_dir():
        raise FileNotFoundError(f"Task directory not found: {task_dir}")
    return TaskSpec.from_task_dir(task_name, task_dir)


def load_task_test_data(task_name: str, config: BenchConfig) -> TaskTestData:
    """Load test configuration for a task from NL2RepoBench."""
    import json

    task_dir = config.test_files_dir / task_name
    if not task_dir.is_dir():
        raise FileNotFoundError(f"Task directory not found: {task_dir}")

    test_commands_path = task_dir / "test_commands.json"
    test_files_path = task_dir / "test_files.json"
    test_case_count_path = task_dir / "test_case_count.txt"

    if not test_commands_path.exists():
        raise FileNotFoundError(f"test_commands.json not found: {test_commands_path}")

    test_commands: list[str] = json.loads(
        test_commands_path.read_text(encoding="utf-8")
    )

    test_files: list[str] = []
    if test_files_path.exists():
        test_files = json.loads(test_files_path.read_text(encoding="utf-8"))

    test_case_count = 0
    if test_case_count_path.exists():
        text = test_case_count_path.read_text(encoding="utf-8").strip()
        if text:
            test_case_count = int(text)

    return TaskTestData(
        task_name=task_name,
        test_commands=test_commands,
        test_files=test_files,
        test_case_count=test_case_count,
    )


def discover_tasks(config: BenchConfig) -> list[str]:
    """Discover all available tasks by scanning test_files/ for dirs with start.md."""
    tasks: list[str] = []
    if not config.test_files_dir.is_dir():
        raise FileNotFoundError(f"Test files directory not found: {config.test_files_dir}")
    for task_dir in sorted(config.test_files_dir.iterdir()):
        if task_dir.is_dir() and (task_dir / "start.md").exists():
            tasks.append(task_dir.name)
    return tasks


def generate_run_id(task_name: str, condition: Condition) -> str:
    """Generate a unique run ID: {task}_{condition}_{YYYYMMDD-HHMMSS}."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{task_name}_{condition.value}_{timestamp}"


def generate_batch_id() -> str:
    """Generate a unique batch ID: batch_{YYYYMMDD-HHMMSS}."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"batch_{timestamp}"
