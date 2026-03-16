"""Generates comparison reports from graded evaluation runs."""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from harness.config import (
    BenchConfig,
    Condition,
    GradeResult,
    QualityScores,
    TestResult,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TaskSummary:
    """Aggregated stats for a single task across conditions."""

    task_name: str
    mean_pass_rate: dict[str, float] = field(default_factory=dict)
    std_pass_rate: dict[str, float] = field(default_factory=dict)
    mean_quality: dict[str, float] = field(default_factory=dict)
    std_quality: dict[str, float] = field(default_factory=dict)
    run_count: dict[str, int] = field(default_factory=dict)


@dataclass
class AggregateStats:
    """Grand aggregate stats across all tasks."""

    overall_mean_pass_rate: dict[str, float] = field(default_factory=dict)
    overall_std_pass_rate: dict[str, float] = field(default_factory=dict)
    overall_mean_quality: dict[str, float] = field(default_factory=dict)
    overall_std_quality: dict[str, float] = field(default_factory=dict)
    wins: int = 0
    ties: int = 0
    losses: int = 0
    total_tasks: int = 0
    total_runs: int = 0


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------


def _mean(values: list[float]) -> float:
    """Compute mean, returning 0.0 for empty lists."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def _std_dev(values: list[float]) -> float:
    """Compute population standard deviation, returning 0.0 for empty/single lists."""
    if len(values) < 2:
        return 0.0
    avg = _mean(values)
    variance = sum((x - avg) ** 2 for x in values) / len(values)
    return math.sqrt(variance)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _load_manifest(batch_id: str, config: BenchConfig) -> dict | None:
    """Load manifest.json for a batch. Returns None on missing/corrupt."""
    manifest_path = config.results_dir / batch_id / "manifest.json"
    if not manifest_path.exists():
        logger.warning("Manifest not found: %s", manifest_path)
        return None
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read manifest %s: %s", manifest_path, e)
        return None


def _load_grade_result(run_id: str, config: BenchConfig) -> GradeResult | None:
    """Load grades.json for a single run. Returns None on missing/corrupt."""
    grades_path = config.results_dir / run_id / "grades.json"
    if not grades_path.exists():
        logger.warning("Grades not found: %s", grades_path)
        return None
    try:
        data = json.loads(grades_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read grades %s: %s", grades_path, e)
        return None

    # Reconstruct TestResult
    test_result: TestResult | None = None
    if data.get("test_result") is not None:
        tr = data["test_result"]
        test_result = TestResult(
            passed=tr["passed"],
            failed=tr["failed"],
            errors=tr["errors"],
            total=tr["total"],
            success_rate=tr["success_rate"],
            command_outputs=tr.get("command_outputs", []),
        )

    # Reconstruct QualityScores
    quality_scores: QualityScores | None = None
    if data.get("quality_scores") is not None:
        qs = data["quality_scores"]
        quality_scores = QualityScores(
            architectural_coherence=qs["architectural_coherence"],
            code_quality=qs["code_quality"],
            completeness=qs["completeness"],
            overall=qs["overall"],
            rationale=qs.get("rationale", ""),
        )

    return GradeResult(
        run_id=data["run_id"],
        task_name=data["task_name"],
        condition=Condition(data["condition"]),
        test_result=test_result,
        quality_scores=quality_scores,
    )


def load_batch_grades(
    batch_id: str, config: BenchConfig
) -> list[GradeResult]:
    """Load all graded results for a batch from disk."""
    manifest = _load_manifest(batch_id, config)
    if manifest is None:
        logger.warning("No manifest for batch %s — returning empty", batch_id)
        return []

    runs = manifest.get("results", manifest.get("runs", []))
    grades: list[GradeResult] = []
    for run_entry in runs:
        run_id = run_entry.get("run_id", "")
        if not run_id:
            continue
        grade = _load_grade_result(run_id, config)
        if grade is not None:
            grades.append(grade)
        else:
            logger.warning("Skipping run %s — no grades found", run_id)

    return grades


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _aggregate_by_task(grades: list[GradeResult]) -> list[TaskSummary]:
    """Group grades by task, compute per-condition stats."""
    # Group: task_name -> condition -> list of grades
    groups: dict[str, dict[str, list[GradeResult]]] = {}
    for g in grades:
        groups.setdefault(g.task_name, {}).setdefault(
            g.condition.value, []
        ).append(g)

    summaries: list[TaskSummary] = []
    for task_name in sorted(groups):
        summary = TaskSummary(task_name=task_name)
        for cond, cond_grades in groups[task_name].items():
            pass_rates = [
                g.test_result.success_rate
                for g in cond_grades
                if g.test_result is not None
            ]
            quality_scores = [
                g.quality_scores.overall
                for g in cond_grades
                if g.quality_scores is not None
            ]
            summary.mean_pass_rate[cond] = _mean(pass_rates)
            summary.std_pass_rate[cond] = _std_dev(pass_rates)
            summary.mean_quality[cond] = _mean(quality_scores)
            summary.std_quality[cond] = _std_dev(quality_scores)
            summary.run_count[cond] = len(cond_grades)
        summaries.append(summary)

    return summaries


def _compute_aggregate_stats(
    task_summaries: list[TaskSummary], grades: list[GradeResult]
) -> AggregateStats:
    """Compute grand means and win/tie/loss counts."""
    stats = AggregateStats()
    stats.total_tasks = len(task_summaries)
    stats.total_runs = len(grades)

    # Collect all pass rates and quality scores per condition across all grades
    cond_pass_rates: dict[str, list[float]] = {}
    cond_quality: dict[str, list[float]] = {}
    for g in grades:
        cv = g.condition.value
        if g.test_result is not None:
            cond_pass_rates.setdefault(cv, []).append(
                g.test_result.success_rate
            )
        if g.quality_scores is not None:
            cond_quality.setdefault(cv, []).append(g.quality_scores.overall)

    for cond in cond_pass_rates:
        stats.overall_mean_pass_rate[cond] = _mean(cond_pass_rates[cond])
        stats.overall_std_pass_rate[cond] = _std_dev(cond_pass_rates[cond])
    for cond in cond_quality:
        stats.overall_mean_quality[cond] = _mean(cond_quality[cond])
        stats.overall_std_quality[cond] = _std_dev(cond_quality[cond])

    # Win/tie/loss: compare baseline vs best non-baseline per task
    baseline = Condition.BASELINE.value
    for ts in task_summaries:
        baseline_rate = ts.mean_pass_rate.get(baseline)
        if baseline_rate is None:
            continue
        # Find best non-baseline condition
        best_other: float | None = None
        for cond, rate in ts.mean_pass_rate.items():
            if cond != baseline:
                if best_other is None or rate > best_other:
                    best_other = rate
        if best_other is None:
            continue
        delta = best_other - baseline_rate
        if abs(delta) < 0.01:
            stats.ties += 1
        elif delta > 0:
            stats.wins += 1
        else:
            stats.losses += 1

    return stats


# ---------------------------------------------------------------------------
# Improvement analysis
# ---------------------------------------------------------------------------


def _analyze_improvements(task_summaries: list[TaskSummary]) -> str:
    """Generate markdown analysis of most improved/regressed tasks."""
    baseline = Condition.BASELINE.value
    deltas: list[tuple[str, float]] = []

    for ts in task_summaries:
        bl = ts.mean_pass_rate.get(baseline)
        if bl is None:
            continue
        # Best non-baseline pass rate
        best_other: float | None = None
        for cond, rate in ts.mean_pass_rate.items():
            if cond != baseline:
                if best_other is None or rate > best_other:
                    best_other = rate
        if best_other is not None:
            deltas.append((ts.task_name, best_other - bl))

    if not deltas:
        return "_No comparison data available (single condition or no baseline)._\n"

    deltas.sort(key=lambda x: x[1], reverse=True)

    lines: list[str] = []

    # Top improved
    improved = [(n, d) for n, d in deltas if d > 0.01]
    if improved:
        lines.append("### Most Improved Tasks\n")
        for name, delta in improved[:5]:
            lines.append(f"- **{name}**: +{delta:.1%}")
        lines.append("")
    else:
        lines.append("### Most Improved Tasks\n")
        lines.append("_No tasks showed meaningful improvement._\n")

    # Top regressed
    regressed = [(n, d) for n, d in reversed(deltas) if d < -0.01]
    if regressed:
        lines.append("### Most Regressed Tasks\n")
        for name, delta in regressed[:5]:
            lines.append(f"- **{name}**: {delta:.1%}")
        lines.append("")
    else:
        lines.append("### Most Regressed Tasks\n")
        lines.append("_No tasks showed meaningful regression._\n")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _render_report(
    batch_id: str,
    task_summaries: list[TaskSummary],
    aggregate: AggregateStats,
    improvement_analysis: str,
    conditions: list[str],
) -> str:
    """Render the full markdown report."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    multi_condition = len(conditions) > 1
    baseline = Condition.BASELINE.value

    lines: list[str] = []
    lines.append(f"# JaRVIS-Bench Report: {batch_id}\n")
    lines.append(f"Generated: {timestamp}\n")

    # --- Summary ---
    lines.append("## Summary\n")
    lines.append(f"- **Tasks evaluated:** {aggregate.total_tasks}")
    lines.append(f"- **Total runs:** {aggregate.total_runs}")
    lines.append(f"- **Conditions:** {', '.join(conditions)}")
    lines.append("")

    # --- Aggregate Results ---
    lines.append("## Aggregate Results\n")

    if multi_condition:
        # Header
        header = "| Metric |"
        sep = "| --- |"
        for c in conditions:
            header += f" {c} |"
            sep += " --- |"
        if baseline in conditions:
            header += " Delta |"
            sep += " --- |"
        lines.append(header)
        lines.append(sep)

        # Pass rate row
        row = "| Pass Rate |"
        bl_pr = aggregate.overall_mean_pass_rate.get(baseline)
        best_non_bl_pr: float | None = None
        for c in conditions:
            val = aggregate.overall_mean_pass_rate.get(c)
            std = aggregate.overall_std_pass_rate.get(c, 0.0)
            if val is not None:
                row += f" {val:.1%} ({std:.1%}) |"
                if c != baseline and (
                    best_non_bl_pr is None or val > best_non_bl_pr
                ):
                    best_non_bl_pr = val
            else:
                row += " N/A |"
        if baseline in conditions:
            if bl_pr is not None and best_non_bl_pr is not None:
                delta = best_non_bl_pr - bl_pr
                sign = "+" if delta >= 0 else ""
                row += f" {sign}{delta:.1%} |"
            else:
                row += " N/A |"
        lines.append(row)

        # Quality row
        row = "| Quality |"
        bl_q = aggregate.overall_mean_quality.get(baseline)
        best_non_bl_q: float | None = None
        for c in conditions:
            val = aggregate.overall_mean_quality.get(c)
            std = aggregate.overall_std_quality.get(c, 0.0)
            if val is not None:
                row += f" {val:.2f} ({std:.2f}) |"
                if c != baseline and (
                    best_non_bl_q is None or val > best_non_bl_q
                ):
                    best_non_bl_q = val
            else:
                row += " N/A |"
        if baseline in conditions:
            if bl_q is not None and best_non_bl_q is not None:
                delta = best_non_bl_q - bl_q
                sign = "+" if delta >= 0 else ""
                row += f" {sign}{delta:.2f} |"
            else:
                row += " N/A |"
        lines.append(row)
        lines.append("")

        # Win/tie/loss
        lines.append(
            f"**Win/Tie/Loss:** {aggregate.wins}W / "
            f"{aggregate.ties}T / {aggregate.losses}L "
            f"(delta threshold: 1%)\n"
        )
    else:
        # Single condition — simple table
        c = conditions[0]
        pr = aggregate.overall_mean_pass_rate.get(c)
        pr_std = aggregate.overall_std_pass_rate.get(c, 0.0)
        q = aggregate.overall_mean_quality.get(c)
        q_std = aggregate.overall_std_quality.get(c, 0.0)
        lines.append("| Metric | Value |")
        lines.append("| --- | --- |")
        lines.append(
            f"| Pass Rate | {pr:.1%} ({pr_std:.1%}) |"
            if pr is not None
            else "| Pass Rate | N/A |"
        )
        lines.append(
            f"| Quality | {q:.2f} ({q_std:.2f}) |"
            if q is not None
            else "| Quality | N/A |"
        )
        lines.append("")

    # --- Per-Task Results ---
    lines.append("## Per-Task Results\n")

    if not task_summaries:
        lines.append("_No graded results found._\n")
    else:
        # Header
        header = "| Task |"
        sep = "| --- |"
        for c in conditions:
            header += f" {c} Pass Rate | {c} Quality |"
            sep += " ---: | ---: |"
        if multi_condition and baseline in conditions:
            header += " Delta |"
            sep += " ---: |"
        lines.append(header)
        lines.append(sep)

        # Sort by delta descending (if multi-condition), else by name
        def _sort_key(ts: TaskSummary) -> float:
            if not multi_condition:
                return 0.0
            bl = ts.mean_pass_rate.get(baseline, 0.0)
            best = max(
                (v for k, v in ts.mean_pass_rate.items() if k != baseline),
                default=bl,
            )
            return -(best - bl)

        for ts in sorted(task_summaries, key=_sort_key):
            row = f"| {ts.task_name} |"
            bl_val = ts.mean_pass_rate.get(baseline)
            best_non_bl: float | None = None
            for c in conditions:
                pr = ts.mean_pass_rate.get(c)
                q = ts.mean_quality.get(c)
                n = ts.run_count.get(c, 0)
                if pr is not None:
                    row += f" {pr:.1%} (n={n}) |"
                else:
                    row += " N/A |"
                if q is not None:
                    row += f" {q:.2f} |"
                else:
                    row += " N/A |"
                if c != baseline and pr is not None:
                    if best_non_bl is None or pr > best_non_bl:
                        best_non_bl = pr
            if multi_condition and baseline in conditions:
                if bl_val is not None and best_non_bl is not None:
                    delta = best_non_bl - bl_val
                    sign = "+" if delta >= 0 else ""
                    row += f" {sign}{delta:.1%} |"
                else:
                    row += " N/A |"
            lines.append(row)
        lines.append("")

    # --- Improvement Analysis ---
    lines.append("## Improvement Analysis\n")
    lines.append(improvement_analysis)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_report(batch_id: str, config: BenchConfig) -> Path:
    """Load graded results from disk and generate a markdown report."""
    grades = load_batch_grades(batch_id, config)
    return generate_report_from_grades(batch_id, grades, config)


def generate_report_from_grades(
    batch_id: str, grades: list[GradeResult], config: BenchConfig
) -> Path:
    """Generate a markdown report from pre-loaded grade results."""
    report_dir = config.results_dir / batch_id
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "report.md"

    if not grades:
        report_path.write_text(
            f"# JaRVIS-Bench Report: {batch_id}\n\n"
            "No graded results found.\n",
            encoding="utf-8",
        )
        logger.info("Wrote empty report to %s", report_path)
        return report_path

    task_summaries = _aggregate_by_task(grades)
    conditions = sorted({g.condition.value for g in grades})
    aggregate = _compute_aggregate_stats(task_summaries, grades)
    improvement_analysis = _analyze_improvements(task_summaries)

    report = _render_report(
        batch_id, task_summaries, aggregate, improvement_analysis, conditions
    )
    report_path.write_text(report, encoding="utf-8")
    logger.info("Wrote report to %s", report_path)
    return report_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate a JaRVIS-Bench comparison report."
    )
    parser.add_argument(
        "--batch-id", required=True, help="Batch ID to report on"
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Project root directory (default: cwd)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    cfg = BenchConfig(project_root=args.project_root)
    try:
        path = generate_report(args.batch_id, cfg)
        print(f"Report written to: {path}")
    except Exception as e:
        logger.error("Failed to generate report: %s", e)
        sys.exit(1)
