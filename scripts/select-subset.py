#!/usr/bin/env python3
"""Select a stratified task subset for cross-model experiments.

Usage: uv run python scripts/select-subset.py --batch-id <batch_id> [--target-size 40] [--dry-run]

Applies the selection algorithm from docs/neurips-experiment-plan.md:
  1. Exclude uninformative tasks (ceiling/floor pass rates)
  2. Compute difficulty tiers from start.md code-block LOC
  3. Validate LOC as a difficulty proxy (Spearman correlation)
  4. Stratified quantile-spread selection within tiers
  5. Flag popular packages

Outputs configs/cross-model-subset.json and prints rationale to stdout.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Import from analyze-results.py (hyphenated filename)
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "analyze_results", _SCRIPT_DIR / "analyze-results.py"
)
assert _spec and _spec.loader
_analyze = importlib.util.module_from_spec(_spec)
sys.modules["analyze_results"] = _analyze
_spec.loader.exec_module(_analyze)

load_data = _analyze.load_data
group_by_task = _analyze.group_by_task
RunRecord = _analyze.RunRecord
TaskStats = _analyze.TaskStats

TRANSIENT_OUTCOMES = {"auth_error", "usage_limit"}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CEILING_THRESHOLD = 0.99
FLOOR_THRESHOLD = 0.02
LOC_THRESHOLDS = (514, 1207)  # easy <= 514, medium 515-1207, hard > 1207
WIN_TIE_THRESHOLD = 0.01

POPULAR_PACKAGES = {
    "six", "jinja", "tqdm", "emoji", "deepdiff", "freezegun",
    "cookiecutter", "boltons", "cerberus", "bleach", "autopep8",
    "rich-click", "stable-baselines3", "aiofiles", "structlog",
    "sortedcontainers", "pytz", "markupsafe", "tenacity",
    "pyjwt", "unidecode",
}

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class TaskMetrics:
    name: str
    baseline_mean: float
    jarvis_mean: float
    delta: float
    n_baseline: int
    n_jarvis: int
    loc: int
    difficulty: str  # "easy", "medium", "hard"
    is_tie: bool
    combined_mean: float  # (baseline + jarvis) / 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def count_code_block_loc(start_md_path: Path) -> int:
    """Count lines inside markdown code blocks (``` delimited) in a file."""
    in_block = False
    loc = 0
    with open(start_md_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("```"):
                in_block = not in_block
                continue
            if in_block:
                loc += 1
    return loc


def _rank(values: list[float]) -> list[float]:
    """Average-rank assignment (handles ties)."""
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j - 1) / 2.0 + 1
        for k in range(i, j):
            ranks[indexed[k][0]] = avg_rank
        i = j
    return ranks


def spearman_r(x: list[float], y: list[float]) -> float:
    """Spearman rank correlation (pure Python, no scipy)."""
    if len(x) < 3:
        return 0.0
    rx, ry = _rank(x), _rank(y)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if sx == 0 or sy == 0:
        return 0.0
    return cov / (sx * sy)


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------


def compute_task_metrics(
    task_stats: dict[str, TaskStats], project_root: Path
) -> dict[str, TaskMetrics]:
    """Compute per-task metrics from genuine runs."""
    nl2repo_dir = project_root / "vendor" / "NL2RepoBench" / "test_files"
    metrics: dict[str, TaskMetrics] = {}

    for name, stats in task_stats.items():
        b_runs = [r for r in stats.baseline if r.outcome not in TRANSIENT_OUTCOMES]
        j_runs = [r for r in stats.jarvis if r.outcome not in TRANSIENT_OUTCOMES]
        if not b_runs or not j_runs:
            continue

        b_mean = _mean([r.pass_rate for r in b_runs])
        j_mean = _mean([r.pass_rate for r in j_runs])
        delta = j_mean - b_mean

        start_md = nl2repo_dir / name / "start.md"
        loc = count_code_block_loc(start_md) if start_md.exists() else 0

        easy_thresh, hard_thresh = LOC_THRESHOLDS
        if loc <= easy_thresh:
            difficulty = "easy"
        elif loc <= hard_thresh:
            difficulty = "medium"
        else:
            difficulty = "hard"

        metrics[name] = TaskMetrics(
            name=name,
            baseline_mean=b_mean,
            jarvis_mean=j_mean,
            delta=delta,
            n_baseline=len(b_runs),
            n_jarvis=len(j_runs),
            loc=loc,
            difficulty=difficulty,
            is_tie=abs(delta) <= WIN_TIE_THRESHOLD,
            combined_mean=(b_mean + j_mean) / 2,
        )

    return metrics


def exclude_uninformative(
    metrics: dict[str, TaskMetrics],
) -> tuple[dict[str, TaskMetrics], list[str], list[str]]:
    """Remove ceiling and floor tasks."""
    ceiling_excluded: list[str] = []
    floor_excluded: list[str] = []
    remaining: dict[str, TaskMetrics] = {}

    for name, m in sorted(metrics.items()):
        if m.baseline_mean >= CEILING_THRESHOLD and m.jarvis_mean >= CEILING_THRESHOLD:
            ceiling_excluded.append(name)
        elif m.baseline_mean <= FLOOR_THRESHOLD and m.jarvis_mean <= FLOOR_THRESHOLD:
            floor_excluded.append(name)
        else:
            remaining[name] = m

    return remaining, ceiling_excluded, floor_excluded


def validate_difficulty_proxy(
    metrics: dict[str, TaskMetrics],
) -> float:
    """Compute Spearman correlation between LOC and mean pass rate."""
    locs = [m.loc for m in metrics.values()]
    rates = [m.combined_mean for m in metrics.values()]
    return spearman_r(locs, rates)


def stratified_select(
    eligible: dict[str, TaskMetrics],
    target_size: int,
    num_controls: int = 4,
) -> list[TaskMetrics]:
    """Select tasks via quantile-spread sampling within difficulty tiers."""
    # Partition into tiers
    tiers: dict[str, list[TaskMetrics]] = {"easy": [], "medium": [], "hard": []}
    for m in eligible.values():
        tiers[m.difficulty].append(m)

    # Sort each tier by combined_mean for quantile-spread
    for tier in tiers.values():
        tier.sort(key=lambda m: m.combined_mean)

    # Compute proportional quotas
    total = sum(len(t) for t in tiers.values())
    raw_quotas: dict[str, int] = {}
    for tier_name, pool in tiers.items():
        raw_quotas[tier_name] = max(8, round(target_size * len(pool) / total))

    # Adjust to hit target
    allocated = sum(raw_quotas.values())
    if allocated != target_size:
        diff = target_size - allocated
        # Adjust the largest tier
        largest = max(tiers, key=lambda t: len(tiers[t]))
        raw_quotas[largest] += diff

    # Cap quotas at pool size
    for tier_name in tiers:
        raw_quotas[tier_name] = min(raw_quotas[tier_name], len(tiers[tier_name]))

    # Reserve negative controls (ties) spread across tiers
    controls: dict[str, list[TaskMetrics]] = {"easy": [], "medium": [], "hard": []}
    controls_remaining = num_controls
    for tier_name in ["medium", "hard", "easy"]:  # medium first (most ties likely)
        ties = [m for m in tiers[tier_name] if m.is_tie]
        # Pick ties near middle of pass-rate range
        ties.sort(key=lambda m: abs(m.combined_mean - 0.5))
        take = min(len(ties), max(1, controls_remaining // 2), controls_remaining)
        if controls_remaining <= 0:
            take = 0
        controls[tier_name] = ties[:take]
        controls_remaining -= take

    # Select from each tier
    selected: list[TaskMetrics] = []
    for tier_name in ["easy", "medium", "hard"]:
        pool = tiers[tier_name]
        quota = raw_quotas[tier_name]
        reserved = controls[tier_name]
        tier_selected = _select_from_tier(pool, quota, reserved)
        selected.extend(tier_selected)

    return selected


def _select_from_tier(
    pool: list[TaskMetrics],
    quota: int,
    reserved_controls: list[TaskMetrics],
) -> list[TaskMetrics]:
    """Quantile-spread selection within a single tier."""
    reserved_names = {m.name for m in reserved_controls}
    available = [m for m in pool if m.name not in reserved_names]
    remaining_quota = quota - len(reserved_controls)

    if remaining_quota <= 0 or not available:
        return reserved_controls[:quota]

    if remaining_quota >= len(available):
        return reserved_controls + available

    # Quantile-spread: pick at evenly spaced indices
    n = len(available)
    indices: list[int] = []
    for i in range(remaining_quota):
        idx = round(i * (n - 1) / (remaining_quota - 1))
        indices.append(idx)

    # Deduplicate
    selected_indices = set(indices)
    while len(selected_indices) < remaining_quota:
        best_idx = -1
        best_dist = -1
        for idx in range(n):
            if idx in selected_indices:
                continue
            min_dist = min(abs(idx - s) for s in selected_indices)
            if min_dist > best_dist:
                best_dist = min_dist
                best_idx = idx
        selected_indices.add(best_idx)

    result = [available[i] for i in sorted(selected_indices)]
    return reserved_controls + result


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def print_rationale(
    all_metrics: dict[str, TaskMetrics],
    eligible: dict[str, TaskMetrics],
    ceiling_excluded: list[str],
    floor_excluded: list[str],
    spearman: float,
    selected: list[TaskMetrics],
    batch_id: str,
) -> None:
    """Print detailed selection rationale to stdout."""
    print("=" * 60)
    print("Task Subset Selection")
    print("=" * 60)
    print(f"Source batch: {batch_id}")
    print(f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Total tasks in batch: {len(all_metrics)}")
    print()

    # Stage 1
    print("--- Stage 1: Exclude uninformative ---")
    print(f"Ceiling excluded ({len(ceiling_excluded)}): {', '.join(ceiling_excluded) or 'none'}")
    print(f"Floor excluded ({len(floor_excluded)}): {', '.join(floor_excluded) or 'none'}")
    print(f"Remaining: {len(eligible)}")
    print()

    # Stage 2: Difficulty tiers
    print("--- Stage 2: Difficulty tiers ---")
    print(f"LOC thresholds: easy <= {LOC_THRESHOLDS[0]}, "
          f"medium {LOC_THRESHOLDS[0]+1}-{LOC_THRESHOLDS[1]}, "
          f"hard > {LOC_THRESHOLDS[1]}")
    for tier in ["easy", "medium", "hard"]:
        tier_tasks = [m for m in eligible.values() if m.difficulty == tier]
        if tier_tasks:
            locs = [m.loc for m in tier_tasks]
            print(f"  {tier.capitalize()}: {len(tier_tasks)} tasks "
                  f"(LOC range: {min(locs)}-{max(locs)})")
    print()

    # Stage 3: Proxy validation
    print("--- Stage 3: Difficulty proxy validation ---")
    print(f"Spearman r(LOC, mean_pass_rate) = {spearman:.3f}")
    if abs(spearman) >= 0.3:
        print("PASS: |r| >= 0.3, LOC is a reasonable difficulty proxy")
    else:
        print("WARN: |r| < 0.3, LOC may be a weak difficulty proxy")
        print("  Consider composite proxy (LOC + test count + Sonnet pass rate)")
    print()

    # Stage 4: Selection
    print("--- Stage 4: Stratified selection ---")
    controls = [m for m in selected if m.is_tie]
    print(f"Target: {len(selected)} tasks, Negative controls: {len(controls)}")
    print()

    for tier in ["easy", "medium", "hard"]:
        tier_selected = [m for m in selected if m.difficulty == tier]
        print(f"{tier.upper()} ({len(tier_selected)} tasks):")
        print(f"  {'Task':<30} {'LOC':>5}  {'Baseline':>8}  {'JaRVIS':>8}  {'Delta':>7}  {'Control':>7}")
        print(f"  {'-'*30} {'-'*5}  {'-'*8}  {'-'*8}  {'-'*7}  {'-'*7}")
        for m in sorted(tier_selected, key=lambda x: x.combined_mean):
            ctrl = "  yes" if m.is_tie else ""
            print(f"  {m.name:<30} {m.loc:>5}  {m.baseline_mean:>7.1%}  "
                  f"{m.jarvis_mean:>7.1%}  {m.delta:>+6.1%}  {ctrl:>7}")
        print()

    # Stage 5: Popular packages
    pop_flags = [m.name for m in selected if m.name in POPULAR_PACKAGES]
    print("--- Stage 5: Popular package flags ---")
    print(f"Flagged ({len(pop_flags)}): {', '.join(sorted(pop_flags)) or 'none'}")
    print()

    # Summary
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    tier_counts = {}
    for tier in ["easy", "medium", "hard"]:
        tier_counts[tier] = len([m for m in selected if m.difficulty == tier])
    print(f"Total selected: {len(selected)}")
    print(f"By tier: {tier_counts['easy']} easy, {tier_counts['medium']} medium, "
          f"{tier_counts['hard']} hard")
    print(f"Negative controls: {len(controls)}")
    print(f"Popular package flags: {len(pop_flags)}")

    # Pass rate coverage
    rates = sorted(m.combined_mean for m in selected)
    print(f"Pass rate range: {rates[0]:.1%} - {rates[-1]:.1%}")
    print(f"Pass rate median: {rates[len(rates)//2]:.1%}")


def write_output_json(
    selected: list[TaskMetrics],
    eligible: dict[str, TaskMetrics],
    ceiling_excluded: list[str],
    floor_excluded: list[str],
    spearman: float,
    batch_id: str,
    target_size: int,
    output_path: Path,
) -> None:
    """Write configs/cross-model-subset.json."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    tasks_json = []
    for m in sorted(selected, key=lambda x: (x.difficulty, x.name)):
        tasks_json.append({
            "name": m.name,
            "difficulty": m.difficulty,
            "start_md_loc": m.loc,
            "sonnet_baseline_pass_rate": round(m.baseline_mean, 4),
            "sonnet_jarvis_pass_rate": round(m.jarvis_mean, 4),
            "sonnet_delta": round(m.delta, 4),
            "sonnet_confidence": "high" if m.n_baseline >= 3 and m.n_jarvis >= 3 else "medium",
            "n_baseline": m.n_baseline,
            "n_jarvis": m.n_jarvis,
            "is_negative_control": m.is_tie,
            "is_popular_package": m.name in POPULAR_PACKAGES,
        })

    output = {
        "tasks": tasks_json,
        "selection_criteria": {
            "ceiling_threshold": CEILING_THRESHOLD,
            "floor_threshold": FLOOR_THRESHOLD,
            "difficulty_thresholds": list(LOC_THRESHOLDS),
            "target_size": target_size,
            "num_negative_controls_target": 4,
        },
        "validation": {
            "spearman_r_loc_passrate": round(spearman, 4),
            "proxy_adequate": abs(spearman) >= 0.3,
        },
        "popularity_flags": sorted(m.name for m in selected if m.name in POPULAR_PACKAGES),
        "excluded_ceiling": ceiling_excluded,
        "excluded_floor": floor_excluded,
        "source_batch": batch_id,
        "selected_at": datetime.now(timezone.utc).isoformat(),
    }

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
        f.write("\n")

    print(f"\nWritten to {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Select task subset for cross-model experiments")
    parser.add_argument("--batch-id", required=True, help="Source Sonnet batch ID")
    parser.add_argument("--target-size", type=int, default=40, help="Number of tasks to select")
    parser.add_argument("--dry-run", action="store_true", help="Print rationale only, don't write JSON")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent

    # Load data
    records = load_data(args.batch_id, project_root)

    # Group by task
    task_stats = group_by_task(records)

    # Compute metrics
    all_metrics = compute_task_metrics(task_stats, project_root)
    print(f"Loaded {len(all_metrics)} paired tasks from {args.batch_id}")

    # Stage 1: Exclude uninformative
    eligible, ceiling_excluded, floor_excluded = exclude_uninformative(all_metrics)

    # Stage 2+3: Difficulty tiers (already computed in compute_task_metrics)
    # Validate proxy
    spearman = validate_difficulty_proxy(eligible)

    # Stage 4: Stratified selection
    selected = stratified_select(eligible, args.target_size)

    # Print rationale
    print_rationale(
        all_metrics, eligible, ceiling_excluded, floor_excluded,
        spearman, selected, args.batch_id,
    )

    # Write output
    if not args.dry_run:
        output_path = project_root / "configs" / "cross-model-subset.json"
        write_output_json(
            selected, eligible, ceiling_excluded, floor_excluded,
            spearman, args.batch_id, args.target_size, output_path,
        )


if __name__ == "__main__":
    main()
