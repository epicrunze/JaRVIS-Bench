#!/usr/bin/env python3
"""Analyze JaRVIS-Bench batch results and generate markdown reports.

Usage: uv run python scripts/analyze-results.py <batch_id>

Produces reports in analysis/{batch_id}/reports/:
  summary.md      — Executive summary with headline numbers
  pass-rates.md   — Per-task pass rate comparison
  efficiency.md   — Cost, time, and token usage analysis
  failures.md     — Failure categorization and impact
  task-details.md — Full per-task run details
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WIN_TIE_THRESHOLD = 0.01  # 1% absolute delta for win/tie/loss

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class RunRecord:
    """Combined data for a single run from manifest + grades."""

    run_id: str
    task_name: str
    condition: str
    outcome: str  # clean, timed_out, auth_error, usage_limit, other_error
    exit_code: int
    timed_out: bool
    wall_clock_seconds: float
    started_at: str
    finished_at: str
    num_turns: int
    cost_usd: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    pass_rate: float
    passed: int
    failed: int
    errors: int
    total_tests: int
    # Diagnostic fields (from grading accuracy fixes)
    skipped: int = 0
    xfailed: int = 0
    collected: int = 0
    expected_total: int = 0
    pip_install_failed: bool = False
    command_timed_out: bool = False


@dataclass
class TaskStats:
    """Aggregated stats for a task per condition."""

    baseline: list[RunRecord] = field(default_factory=list)
    jarvis: list[RunRecord] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def _std_dev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    avg = _mean(values)
    variance = sum((x - avg) ** 2 for x in values) / len(values)
    return math.sqrt(variance)


def _cohens_d(group1: list[float], group2: list[float]) -> float:
    """Cohen's d effect size (group2 - group1)."""
    if len(group1) < 2 or len(group2) < 2:
        return 0.0
    m1, m2 = _mean(group1), _mean(group2)
    s1 = _std_dev(group1)
    s2 = _std_dev(group2)
    pooled = math.sqrt((s1**2 + s2**2) / 2)
    if pooled == 0:
        return 0.0
    return (m2 - m1) / pooled


def _pct(v: float) -> str:
    return f"{v:.1%}"


def _usd(v: float) -> str:
    return f"${v:.2f}"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _classify_outcome(run: dict) -> str:
    stdout = (run.get("raw_stdout", "") or "").lower()
    if "authentication_error" in stdout:
        return "auth_error"
    if "hit your limit" in stdout or "you've hit" in stdout:
        return "usage_limit"
    if run.get("timed_out"):
        return "timed_out"
    if run.get("exit_code", 0) != 0:
        return "other_error"
    return "clean"


def _extract_claude_output(run: dict) -> dict:
    """Extract claude_output, parsing from raw_stdout if needed."""
    co = run.get("claude_output")
    if co and isinstance(co, dict):
        return co
    stdout = run.get("raw_stdout", "") or ""
    if stdout.strip():
        try:
            return json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def load_data(batch_id: str, project_root: Path) -> list[RunRecord]:
    """Load manifest + grades for all runs in a batch."""
    manifest_path = project_root / "results" / batch_id / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest not found at {manifest_path}", file=sys.stderr)
        sys.exit(1)

    with open(manifest_path) as f:
        manifest = json.load(f)

    records = []
    for run in manifest.get("runs", []):
        run_id = run["run_id"]
        outcome = _classify_outcome(run)

        # Load grades
        grades_path = project_root / "results" / run_id / "grades.json"
        pass_rate = 0.0
        passed = failed = errors = total_tests = 0
        skipped = 0
        xfailed = 0
        collected_count = 0
        expected_total = 0
        pip_install_failed = False
        cmd_timed_out = False

        if grades_path.exists():
            with open(grades_path) as f:
                grades = json.load(f)
            tr = grades.get("test_result")
            if tr:
                pass_rate = tr.get("success_rate", 0.0)
                passed = tr.get("passed", 0)
                failed = tr.get("failed", 0)
                errors = tr.get("errors", 0)
                total_tests = tr.get("total", 0)
                skipped = tr.get("skipped", 0)
                xfailed = tr.get("xfailed", 0)
                collected_count = tr.get("collected", 0)
                expected_total = tr.get("expected_total", 0)
                pip_install_failed = tr.get("pip_install_failed", False)
                cmd_timed_out = tr.get("command_timed_out", False)

        # Extract metrics from claude_output
        co = _extract_claude_output(run)
        usage = co.get("usage", {})

        records.append(RunRecord(
            run_id=run_id,
            task_name=run["task_name"],
            condition=run["condition"],
            outcome=outcome,
            exit_code=run.get("exit_code", -1),
            timed_out=run.get("timed_out", False),
            wall_clock_seconds=run.get("wall_clock_seconds", 0.0),
            started_at=run.get("started_at", ""),
            finished_at=run.get("finished_at", ""),
            num_turns=co.get("num_turns", 0),
            cost_usd=co.get("total_cost_usd", 0.0) or 0.0,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
            pass_rate=pass_rate,
            passed=passed,
            failed=failed,
            errors=errors,
            total_tests=total_tests,
            skipped=skipped,
            xfailed=xfailed,
            collected=collected_count,
            expected_total=expected_total,
            pip_install_failed=pip_install_failed,
            command_timed_out=cmd_timed_out,
        ))

    return records


def group_by_task(records: list[RunRecord]) -> dict[str, TaskStats]:
    """Group records by task name, split by condition."""
    tasks: dict[str, TaskStats] = {}
    for r in records:
        if r.task_name not in tasks:
            tasks[r.task_name] = TaskStats()
        ts = tasks[r.task_name]
        if r.condition == "baseline":
            ts.baseline.append(r)
        else:
            ts.jarvis.append(r)
    return tasks


# ---------------------------------------------------------------------------
# Report generators
# ---------------------------------------------------------------------------


# Transient infrastructure failures — excluded from comparison.
# All other outcomes (clean, timed_out, other_error) are genuine results.
TRANSIENT_OUTCOMES = {"auth_error", "usage_limit"}


def _is_genuine(r: RunRecord) -> bool:
    """True if this run represents a genuine result (not an infrastructure failure)."""
    return r.outcome not in TRANSIENT_OUTCOMES


def _confidence(baseline_n: int, jarvis_n: int) -> str:
    if baseline_n >= 3 and jarvis_n >= 3:
        return "high"
    if baseline_n >= 2 and jarvis_n >= 2:
        return "medium"
    return "low"


def generate_summary(records: list[RunRecord], batch_id: str) -> str:
    """Report 1: Executive summary."""
    genuine = [r for r in records if _is_genuine(r)]
    tasks = group_by_task(genuine)

    # Paired tasks (both conditions have genuine runs)
    paired = {t: s for t, s in tasks.items() if s.baseline and s.jarvis}

    b_rates = [_mean([r.pass_rate for r in s.baseline]) for s in paired.values()]
    j_rates = [_mean([r.pass_rate for r in s.jarvis]) for s in paired.values()]

    wins = ties = losses = 0
    for b, j in zip(b_rates, j_rates):
        d = j - b
        if d > WIN_TIE_THRESHOLD:
            wins += 1
        elif d < -WIN_TIE_THRESHOLD:
            losses += 1
        else:
            ties += 1

    # Flat pass rates for effect size
    all_b = [r.pass_rate for r in genuine if r.condition == "baseline"]
    all_j = [r.pass_rate for r in genuine if r.condition != "baseline"]
    effect = _cohens_d(all_b, all_j)

    # Outcome counts
    outcomes = defaultdict(int)
    cond_outcomes = defaultdict(lambda: defaultdict(int))
    for r in records:
        outcomes[r.outcome] += 1
        cond_outcomes[r.condition][r.outcome] += 1

    lines = [
        f"# Batch Summary — {batch_id}",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## Run Outcomes",
        "",
        f"| | Total | Baseline | JaRVIS |",
        f"|---|---:|---:|---:|",
    ]
    for outcome in ["clean", "timed_out", "auth_error", "usage_limit", "other_error"]:
        total = outcomes.get(outcome, 0)
        b = cond_outcomes.get("baseline", {}).get(outcome, 0)
        j = cond_outcomes.get("jarvis-prompted", {}).get(outcome, 0)
        label = outcome.replace("_", " ").title()
        lines.append(f"| {label} | {total} | {b} | {j} |")
    lines.append(f"| **Total** | **{len(records)}** | **{sum(cond_outcomes.get('baseline', {}).values())}** | **{sum(cond_outcomes.get('jarvis-prompted', {}).values())}** |")

    lines += [
        "",
        "## Headline Results (Genuine Runs — excludes auth/usage-limit errors)",
        "",
        f"| Metric | Baseline | JaRVIS | Delta |",
        f"|---|---:|---:|---:|",
        f"| Tasks compared | {len(paired)} | {len(paired)} | — |",
        f"| Genuine runs | {len(all_b)} | {len(all_j)} | — |",
        f"| Mean pass rate | {_pct(_mean(b_rates))} | {_pct(_mean(j_rates))} | {_pct(_mean(j_rates) - _mean(b_rates))} |",
        f"| Median pass rate | {_pct(_median(b_rates))} | {_pct(_median(j_rates))} | {_pct(_median(j_rates) - _median(b_rates))} |",
        f"| Std dev | {_pct(_std_dev(b_rates))} | {_pct(_std_dev(j_rates))} | — |",
        f"| Cohen's d | — | — | {effect:.3f} |",
        "",
        "## Win/Tie/Loss",
        "",
        f"| JaRVIS Wins | Ties | Baseline Wins |",
        f"|---:|---:|---:|",
        f"| {wins} ({wins/len(paired)*100:.0f}%) | {ties} ({ties/len(paired)*100:.0f}%) | {losses} ({losses/len(paired)*100:.0f}%) |",
        "",
        f"*Threshold: {WIN_TIE_THRESHOLD:.0%} absolute delta. {len(paired)} paired tasks with genuine runs in both conditions.*",
        "",
    ]

    # Confidence breakdown
    conf_counts = defaultdict(int)
    for t, s in paired.items():
        conf_counts[_confidence(len(s.baseline), len(s.jarvis))] += 1
    lines += [
        "## Data Confidence",
        "",
        f"| Confidence | Tasks | Description |",
        f"|---|---:|---|",
        f"| High | {conf_counts['high']} | 3 runs per condition |",
        f"| Medium | {conf_counts['medium']} | 2+ runs per condition |",
        f"| Low | {conf_counts['low']} | 1 run in at least one condition |",
        "",
    ]

    # Unpaired tasks
    baseline_only = [t for t, s in tasks.items() if s.baseline and not s.jarvis]
    jarvis_only = [t for t, s in tasks.items() if s.jarvis and not s.baseline]
    if baseline_only or jarvis_only:
        lines += [
            "## Unpaired Tasks (excluded from comparison)",
            "",
        ]
        if baseline_only:
            lines.append(f"- Baseline only ({len(baseline_only)}): {', '.join(sorted(baseline_only))}")
        if jarvis_only:
            lines.append(f"- JaRVIS only ({len(jarvis_only)}): {', '.join(sorted(jarvis_only))}")
        lines.append("")

    return "\n".join(lines)


def generate_pass_rates(records: list[RunRecord]) -> str:
    """Report 2: Per-task pass rate comparison."""
    genuine = [r for r in records if _is_genuine(r)]
    tasks = group_by_task(genuine)
    paired = {t: s for t, s in tasks.items() if s.baseline and s.jarvis}

    rows = []
    for task, s in paired.items():
        b_mean = _mean([r.pass_rate for r in s.baseline])
        j_mean = _mean([r.pass_rate for r in s.jarvis])
        delta = j_mean - b_mean
        conf = _confidence(len(s.baseline), len(s.jarvis))
        rows.append((task, b_mean, len(s.baseline), j_mean, len(s.jarvis), delta, conf))

    rows.sort(key=lambda x: x[5], reverse=True)

    wins = [r for r in rows if r[5] > WIN_TIE_THRESHOLD]
    ties = [r for r in rows if abs(r[5]) <= WIN_TIE_THRESHOLD]
    losses = [r for r in rows if r[5] < -WIN_TIE_THRESHOLD]

    def _table(section_rows: list) -> list[str]:
        lines = [
            "| Task | Baseline | n | JaRVIS | n | Delta | Conf |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
        for task, b, bn, j, jn, d, c in section_rows:
            lines.append(f"| {task} | {_pct(b)} | {bn} | {_pct(j)} | {jn} | {_pct(d)} | {c} |")
        return lines

    lines = [
        "# Pass Rate Comparison",
        "",
        f"*{len(paired)} tasks with clean runs in both conditions.*",
        "",
        f"## JaRVIS Wins ({len(wins)} tasks)",
        "",
    ]
    lines += _table(wins)
    lines += [
        "",
        f"## Ties ({len(ties)} tasks)",
        "",
    ]
    lines += _table(ties)
    lines += [
        "",
        f"## Baseline Wins ({len(losses)} tasks)",
        "",
    ]
    lines += _table(losses)

    # Confidence summary
    conf_counts = defaultdict(int)
    for r in rows:
        conf_counts[r[6]] += 1
    lines += [
        "",
        "## Confidence Summary",
        "",
        f"- High (3+3 runs): {conf_counts['high']} tasks",
        f"- Medium (2+2 runs): {conf_counts['medium']} tasks",
        f"- Low (1 run either side): {conf_counts['low']} tasks",
    ]

    return "\n".join(lines)


def generate_efficiency(records: list[RunRecord]) -> str:
    """Report 3: Cost and efficiency analysis."""
    genuine = [r for r in records if _is_genuine(r)]
    b_runs = [r for r in genuine if r.condition == "baseline"]
    j_runs = [r for r in genuine if r.condition != "baseline"]

    def _agg(runs: list[RunRecord]) -> dict:
        return {
            "count": len(runs),
            "wall_mean": _mean([r.wall_clock_seconds for r in runs]),
            "wall_median": _median([r.wall_clock_seconds for r in runs]),
            "turns_mean": _mean([float(r.num_turns) for r in runs]),
            "turns_median": _median([float(r.num_turns) for r in runs]),
            "cost_mean": _mean([r.cost_usd for r in runs]),
            "cost_median": _median([r.cost_usd for r in runs]),
            "cost_total": sum(r.cost_usd for r in runs),
            "input_mean": _mean([float(r.input_tokens) for r in runs]),
            "output_mean": _mean([float(r.output_tokens) for r in runs]),
            "cache_read_mean": _mean([float(r.cache_read_tokens) for r in runs]),
            "cache_create_mean": _mean([float(r.cache_creation_tokens) for r in runs]),
            "total_input_mean": _mean([float(r.input_tokens + r.cache_read_tokens + r.cache_creation_tokens) for r in runs]),
            "pass_rate_mean": _mean([r.pass_rate for r in runs]),
        }

    ba = _agg(b_runs)
    ja = _agg(j_runs)

    def _cost_per_pp(agg: dict) -> str:
        if agg["pass_rate_mean"] == 0:
            return "N/A"
        return _usd(agg["cost_total"] / (agg["pass_rate_mean"] * 100))

    lines = [
        "# Efficiency Analysis",
        "",
        "## Aggregate Metrics",
        "",
        "| Metric | Baseline | JaRVIS |",
        "|---|---:|---:|",
        f"| Clean runs | {ba['count']} | {ja['count']} |",
        f"| Wall time (mean) | {ba['wall_mean']:.0f}s | {ja['wall_mean']:.0f}s |",
        f"| Wall time (median) | {ba['wall_median']:.0f}s | {ja['wall_median']:.0f}s |",
        f"| Turns (mean) | {ba['turns_mean']:.1f} | {ja['turns_mean']:.1f} |",
        f"| Turns (median) | {ba['turns_median']:.0f} | {ja['turns_median']:.0f} |",
        f"| Cost/run (mean) | {_usd(ba['cost_mean'])} | {_usd(ja['cost_mean'])} |",
        f"| Cost/run (median) | {_usd(ba['cost_median'])} | {_usd(ja['cost_median'])} |",
        f"| Total cost | {_usd(ba['cost_total'])} | {_usd(ja['cost_total'])} |",
        f"| Total input tokens (mean) | {ba['total_input_mean']:,.0f} | {ja['total_input_mean']:,.0f} |",
        f"| — of which cache read | {ba['cache_read_mean']:,.0f} | {ja['cache_read_mean']:,.0f} |",
        f"| — of which cache create | {ba['cache_create_mean']:,.0f} | {ja['cache_create_mean']:,.0f} |",
        f"| — of which uncached | {ba['input_mean']:,.0f} | {ja['input_mean']:,.0f} |",
        f"| Output tokens (mean) | {ba['output_mean']:,.0f} | {ja['output_mean']:,.0f} |",
        f"| Mean pass rate | {_pct(ba['pass_rate_mean'])} | {_pct(ja['pass_rate_mean'])} |",
        f"| Cost per pass-rate point | {_cost_per_pp(ba)} | {_cost_per_pp(ja)} |",
        "",
    ]

    # Per-task efficiency
    tasks = group_by_task(genuine)
    paired = {t: s for t, s in tasks.items() if s.baseline and s.jarvis}

    task_rows = []
    for task, s in sorted(paired.items()):
        b_time = _mean([r.wall_clock_seconds for r in s.baseline])
        j_time = _mean([r.wall_clock_seconds for r in s.jarvis])
        b_cost = _mean([r.cost_usd for r in s.baseline])
        j_cost = _mean([r.cost_usd for r in s.jarvis])
        b_pass = _mean([r.pass_rate for r in s.baseline])
        j_pass = _mean([r.pass_rate for r in s.jarvis])
        task_rows.append((task, b_time, j_time, b_cost, j_cost, b_pass, j_pass))

    lines += [
        "## Per-Task Efficiency",
        "",
        "| Task | B Time | J Time | B Cost | J Cost | B Pass | J Pass |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for task, bt, jt, bc, jc, bp, jp in task_rows:
        lines.append(f"| {task} | {bt:.0f}s | {jt:.0f}s | {_usd(bc)} | {_usd(jc)} | {_pct(bp)} | {_pct(jp)} |")

    # Double wins: JaRVIS better on both pass rate AND cost
    double_wins = [(t, bp, jp, bc, jc) for t, bt, jt, bc, jc, bp, jp in task_rows
                   if jp > bp + WIN_TIE_THRESHOLD and jc < bc]
    double_losses = [(t, bp, jp, bc, jc) for t, bt, jt, bc, jc, bp, jp in task_rows
                     if bp > jp + WIN_TIE_THRESHOLD and bc < jc]

    lines += [
        "",
        "## Efficiency + Quality Wins",
        "",
        f"**JaRVIS wins on both pass rate AND lower cost:** {len(double_wins)} tasks",
    ]
    if double_wins:
        for t, bp, jp, bc, jc in double_wins:
            lines.append(f"- {t}: pass {_pct(bp)}→{_pct(jp)}, cost {_usd(bc)}→{_usd(jc)}")

    lines += [
        "",
        f"**Baseline wins on both pass rate AND lower cost:** {len(double_losses)} tasks",
    ]
    if double_losses:
        for t, bp, jp, bc, jc in double_losses:
            lines.append(f"- {t}: pass {_pct(jp)}→{_pct(bp)}, cost {_usd(jc)}→{_usd(bc)}")

    return "\n".join(lines)


def generate_failures(records: list[RunRecord], batch_id: str) -> str:
    """Report 4: Failure analysis."""
    lines = [
        f"# Failure Analysis — {batch_id}",
        "",
        "## Failure Distribution",
        "",
        "| Outcome | Total | % | Baseline | JaRVIS |",
        "|---|---:|---:|---:|---:|",
    ]

    cond_outcomes = defaultdict(lambda: defaultdict(int))
    outcome_totals = defaultdict(int)
    for r in records:
        outcome_totals[r.outcome] += 1
        cond_outcomes[r.condition][r.outcome] += 1

    for outcome in ["clean", "timed_out", "auth_error", "usage_limit", "other_error"]:
        total = outcome_totals.get(outcome, 0)
        pct = total / len(records) * 100 if records else 0
        b = cond_outcomes.get("baseline", {}).get(outcome, 0)
        j = cond_outcomes.get("jarvis-prompted", {}).get(outcome, 0)
        label = outcome.replace("_", " ").title()
        lines.append(f"| {label} | {total} | {pct:.1f}% | {b} | {j} |")

    # Timeline clustering
    failed = [r for r in records if r.outcome != "clean"]
    if failed:
        by_time = sorted(failed, key=lambda r: r.started_at)
        lines += [
            "",
            "## Failure Timeline",
            "",
            f"First failure: {by_time[0].started_at}",
            f"Last failure: {by_time[-1].started_at}",
            "",
        ]

        # Group by hour
        hour_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for r in failed:
            if r.started_at:
                hour = r.started_at[:13]  # YYYY-MM-DDTHH
                hour_counts[hour][r.outcome] += 1

        lines += [
            "### Failures by Hour",
            "",
            "| Hour (UTC) | Timed Out | Auth | Usage Limit | Other |",
            "|---|---:|---:|---:|---:|",
        ]
        for hour in sorted(hour_counts):
            hc = hour_counts[hour]
            lines.append(f"| {hour} | {hc.get('timed_out', 0)} | {hc.get('auth_error', 0)} | {hc.get('usage_limit', 0)} | {hc.get('other_error', 0)} |")

    # Most affected tasks
    task_failures: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in records:
        task_failures[r.task_name][r.outcome] += 1

    worst_tasks = sorted(task_failures.items(),
                         key=lambda x: sum(v for k, v in x[1].items() if k != "clean"),
                         reverse=True)

    lines += [
        "",
        "## Most Affected Tasks",
        "",
        "| Task | Clean | Timed Out | Auth | Usage | Other | Total Failures |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for task, oc in worst_tasks[:20]:
        total_fail = sum(v for k, v in oc.items() if k != "clean")
        if total_fail == 0:
            continue
        lines.append(
            f"| {task} | {oc.get('clean', 0)} | {oc.get('timed_out', 0)} | "
            f"{oc.get('auth_error', 0)} | {oc.get('usage_limit', 0)} | "
            f"{oc.get('other_error', 0)} | {total_fail} |"
        )

    # Skew assessment: tasks where failures differentially affect one condition
    lines += [
        "",
        "## Skew Assessment",
        "",
        "*Tasks where failures may bias the comparison (unequal clean runs between conditions):*",
        "",
        "| Task | Baseline Clean | JaRVIS Clean | Skew |",
        "|---|---:|---:|---|",
    ]
    tasks = group_by_task(records)
    skewed = []
    for task in sorted(tasks):
        s = tasks[task]
        b_clean = sum(1 for r in s.baseline if r.outcome == "clean")
        j_clean = sum(1 for r in s.jarvis if r.outcome == "clean")
        if b_clean != j_clean:
            direction = "favors JaRVIS" if j_clean > b_clean else "favors Baseline"
            skewed.append((task, b_clean, j_clean, direction))
            lines.append(f"| {task} | {b_clean} | {j_clean} | {direction} |")

    if not skewed:
        lines.append("| *(none)* | — | — | — |")

    # Incomplete data
    lines += [
        "",
        "## Incomplete Data",
        "",
        "*Tasks with fewer than 3 clean runs per condition:*",
        "",
    ]
    incomplete = []
    for task in sorted(tasks):
        s = tasks[task]
        b_clean = sum(1 for r in s.baseline if r.outcome == "clean")
        j_clean = sum(1 for r in s.jarvis if r.outcome == "clean")
        if b_clean < 3 or j_clean < 3:
            incomplete.append((task, b_clean, j_clean))

    if incomplete:
        lines += [
            "| Task | Baseline Clean | JaRVIS Clean |",
            "|---|---:|---:|",
        ]
        for task, bc, jc in incomplete:
            lines.append(f"| {task} | {bc} | {jc} |")
        lines.append(f"\n*{len(incomplete)} of {len(tasks)} tasks have incomplete data.*")
    else:
        lines.append("All tasks have 3 clean runs per condition.")

    return "\n".join(lines)


def generate_task_details(records: list[RunRecord]) -> str:
    """Report 5: Full per-task detail."""
    tasks = group_by_task(records)

    lines = [
        "# Task Details",
        "",
        f"*{len(tasks)} tasks, {len(records)} total runs.*",
        "",
    ]

    for task in sorted(tasks):
        s = tasks[task]
        all_runs = sorted(s.baseline + s.jarvis, key=lambda r: (r.condition, r.run_id))

        lines += [
            f"## {task}",
            "",
            "| Run ID | Condition | Outcome | Pass Rate | Passed/Total | Time | Cost | Turns |",
            "|---|---|---|---:|---:|---:|---:|---:|",
        ]

        for r in all_runs:
            rid_short = r.run_id.split("_")[-1]  # just the hash suffix
            lines.append(
                f"| ...{rid_short} | {r.condition} | {r.outcome} | "
                f"{_pct(r.pass_rate)} | {r.passed}/{r.total_tests} | "
                f"{r.wall_clock_seconds:.0f}s | {_usd(r.cost_usd)} | {r.num_turns} |"
            )

        # Anomaly notes
        anomalies = []
        pass_rates = [r.pass_rate for r in all_runs if r.outcome == "clean"]
        if pass_rates and (max(pass_rates) - min(pass_rates)) > 0.5:
            anomalies.append(f"High variance in pass rates ({_pct(min(pass_rates))} to {_pct(max(pass_rates))})")
        failed_count = sum(1 for r in all_runs if r.outcome != "clean")
        if failed_count > 0:
            anomalies.append(f"{failed_count} of {len(all_runs)} runs had issues")

        if anomalies:
            lines.append("")
            for a in anomalies:
                lines.append(f"*Note: {a}*")

        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: uv run python scripts/analyze-results.py <batch_id>", file=sys.stderr)
        sys.exit(1)

    batch_id = sys.argv[1]
    project_root = Path(__file__).resolve().parent.parent

    print(f"Loading data for {batch_id}...")
    records = load_data(batch_id, project_root)
    print(f"Loaded {len(records)} runs")

    output_dir = project_root / "analysis" / batch_id / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)

    reports = [
        ("summary.md", generate_summary(records, batch_id)),
        ("pass-rates.md", generate_pass_rates(records)),
        ("efficiency.md", generate_efficiency(records)),
        ("failures.md", generate_failures(records, batch_id)),
        ("task-details.md", generate_task_details(records)),
    ]

    for filename, content in reports:
        path = output_dir / filename
        path.write_text(content)
        print(f"  Wrote {path}")

    print("Done.")


if __name__ == "__main__":
    main()
