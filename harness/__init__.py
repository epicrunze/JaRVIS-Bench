"""JaRVIS-Bench: A/B evaluation framework for Claude Code with reflective journaling."""

from harness.config import (
    BenchConfig,
    Condition,
    GradeResult,
    QualityScores,
    RunResult,
    TaskSpec,
    TaskTestData,
    TestResult,
    load_batch_result,
    load_run_result,
)
from harness.docker import check_docker_available
from harness.grader import grade_batch, grade_run
from harness.reporter import generate_report, generate_report_from_grades
from harness.runner import run_evaluation, run_full_benchmark, run_task

__all__ = [
    "BenchConfig",
    "Condition",
    "GradeResult",
    "QualityScores",
    "RunResult",
    "TaskSpec",
    "TaskTestData",
    "TestResult",
    "check_docker_available",
    "generate_report",
    "generate_report_from_grades",
    "grade_batch",
    "grade_run",
    "load_batch_result",
    "load_run_result",
    "run_evaluation",
    "run_full_benchmark",
    "run_task",
]
