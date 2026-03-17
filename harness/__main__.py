"""CLI entry point for JaRVIS-Bench: python -m harness."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TextIO

import click

from harness.config import (
    BenchConfig,
    Condition,
    discover_tasks,
    load_batch_result,
    load_run_result,
)
from harness.grader import grade_batch, grade_run
from harness.reporter import generate_report
from harness.runner import run_full_benchmark

logger = logging.getLogger("harness")

# Smoke-test defaults: 1 easy task, 1 run each condition
SMOKE_TASK = "graphneuralnetwork"


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable DEBUG logging.")
def cli(verbose: bool) -> None:
    """JaRVIS-Bench: A/B evaluation framework for Claude Code."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--task", "task_name", type=str, default=None, help="Single task name.")
@click.option(
    "--condition",
    type=click.Choice(["baseline", "jarvis-prompted", "both"], case_sensitive=False),
    default="both",
    help="Condition(s) to run.",
)
@click.option("--runs", type=int, default=3, help="Runs per task×condition.")
@click.option("--full", "full_flag", is_flag=True, help="Run all 104 tasks.")
@click.option("--smoke", "smoke_flag", is_flag=True, help="Quick smoke test.")
@click.option("--timeout", type=int, default=3600, help="Per-task timeout (seconds).")
@click.option("--max-turns", type=int, default=None, help="Max conversation turns for Claude.")
@click.option("--max-budget-usd", type=float, default=None, help="Max budget in USD per task.")
@click.option("--model", type=str, default=None, help="Claude model override.")
@click.option("--parallel", type=int, default=1, help="Max concurrent runs (default: 1 = sequential).")
@click.option("--no-docker", is_flag=True, help="Run Claude without Docker isolation.")
@click.option(
    "--project-root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Project root (default: cwd).",
)
@click.option(
    "--tasks-from",
    type=click.File("r"),
    default=None,
    help="Read task names from file (one per line).",
)
def run(
    task_name: str | None,
    condition: str,
    runs: int,
    full_flag: bool,
    smoke_flag: bool,
    timeout: int,
    max_turns: int | None,
    max_budget_usd: float | None,
    model: str | None,
    parallel: int,
    no_docker: bool,
    project_root: Path | None,
    tasks_from: TextIO | None,
) -> None:
    """Run evaluation(s) on NL2Repo-Bench tasks."""
    # Validate: exactly one source
    sources = sum([task_name is not None, full_flag, smoke_flag, tasks_from is not None])
    if sources != 1:
        raise click.UsageError(
            "Specify exactly one of --task, --full, --smoke, or --tasks-from."
        )

    root = project_root or Path.cwd()

    # Resolve conditions
    if condition == "both":
        conditions = [Condition.BASELINE, Condition.JARVIS_PROMPTED]
    else:
        conditions = [Condition(condition)]

    # Build config (tasks resolved below)
    config = BenchConfig(
        project_root=root,
        conditions=conditions,
        num_runs=runs,
        timeout_seconds=timeout,
        max_budget_usd=max_budget_usd,
        max_turns=max_turns,
        use_docker=not no_docker,
        max_workers=parallel,
    )
    if model:
        config.model = model

    # Validate config
    validation_errors = config.validate()
    if validation_errors:
        for err in validation_errors:
            click.echo(f"Error: {err}", err=True)
        raise click.Abort()

    # Resolve task list
    if smoke_flag:
        config.tasks = [SMOKE_TASK]
        config.num_runs = 1
    elif full_flag:
        config.tasks = discover_tasks(config)
    elif task_name:
        config.tasks = [task_name]
    elif tasks_from is not None:
        lines = [line.strip() for line in tasks_from]
        config.tasks = [t for t in lines if t and not t.startswith("#")]

    batch = run_full_benchmark(config)

    # Report completion stats
    total_runs = len(batch.results)
    assert config.tasks is not None
    expected_runs = len(config.tasks) * len(config.conditions) * config.num_runs
    failed_runs = sum(1 for r in batch.results if r.error is not None)
    timed_out_runs = sum(1 for r in batch.results if r.timed_out)

    click.echo(f"Batch complete: {batch.batch_id} ({total_runs}/{expected_runs} runs)")
    if failed_runs:
        click.echo(f"  {failed_runs} run(s) failed with errors")
    if timed_out_runs:
        click.echo(f"  {timed_out_runs} run(s) timed out")


# ---------------------------------------------------------------------------
# grade
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--run-id", type=str, default=None, help="Grade a single run.")
@click.option("--batch-id", type=str, default=None, help="Grade all runs in a batch.")
@click.option(
    "--project-root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Project root (default: cwd).",
)
def grade(
    run_id: str | None,
    batch_id: str | None,
    project_root: Path | None,
) -> None:
    """Grade completed evaluation run(s)."""
    if not run_id and not batch_id:
        raise click.UsageError("Specify --run-id or --batch-id.")
    if run_id and batch_id:
        raise click.UsageError("Specify only one of --run-id or --batch-id.")

    root = project_root or Path.cwd()
    config = BenchConfig(project_root=root)

    if run_id:
        result = load_run_result(run_id, config)
        grade_result = grade_run(result, config)
        click.echo(f"Graded run {run_id}: pass_rate={_pass_rate(grade_result)}")
    else:
        assert batch_id is not None
        batch = load_batch_result(batch_id, config)
        grades = grade_batch(batch, config)
        click.echo(f"Graded batch {batch_id}: {len(grades)} runs")


def _pass_rate(grade_result: object) -> str:
    """Format pass rate from a GradeResult for display."""
    from harness.config import GradeResult

    if not isinstance(grade_result, GradeResult):
        return "N/A"
    if grade_result.test_result:
        return f"{grade_result.test_result.success_rate:.1%}"
    return "N/A"


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--batch-id", required=True, type=str, help="Batch ID to report on.")
@click.option(
    "--project-root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Project root (default: cwd).",
)
def report(batch_id: str, project_root: Path | None) -> None:
    """Generate a comparison report for a batch."""
    root = project_root or Path.cwd()
    config = BenchConfig(project_root=root)
    report_path = generate_report(batch_id, config)
    click.echo(f"Report written to {report_path}")


if __name__ == "__main__":
    cli()
