"""Data preparation and persistence for hierarchical batch analysis."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from harness.config import (
    BenchConfig,
    load_task_spec,
)
from harness.reporter import _load_grade_result, _load_manifest

logger = logging.getLogger(__name__)

# Directories/files to skip when reading workspace files
_SKIP_DIRS = {".claude", ".jarvis", "__pycache__", ".git", ".venv", "node_modules"}

# Binary file extensions to skip
_BINARY_EXTENSIONS = {
    ".pyc", ".pyo", ".so", ".o", ".a", ".dylib", ".dll",
    ".exe", ".bin", ".pkl", ".pickle", ".npy", ".npz",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".whl", ".egg", ".db", ".sqlite", ".sqlite3",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
}


def _read_workspace_files(
    workspace_path: Path, max_file_size: int = 100_000
) -> dict[str, str]:
    """Read text files from workspace, skipping binary and internal dirs."""
    files: dict[str, str] = {}

    for path in sorted(workspace_path.rglob("*")):
        if not path.is_file():
            continue

        rel = path.relative_to(workspace_path)
        if rel.parts and rel.parts[0] in _SKIP_DIRS:
            continue
        if path.suffix.lower() in _BINARY_EXTENSIONS:
            continue

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            if len(content) > max_file_size:
                content = content[:max_file_size] + "\n... [truncated]"
            files[str(rel)] = content
        except Exception:
            logger.debug("Could not read file: %s", path)

    return files


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class RunAnalysisContext:
    """All data needed for a tier-1 per-run analysis."""

    run_id: str
    task_name: str
    condition: str
    pass_rate: float
    passed: int
    failed: int
    errors: int
    total: int
    quality_scores: dict[str, float] | None
    quality_rationale: str
    test_output: str
    spec_content: str
    workspace_summary: str
    plan_content: str | None


@dataclass
class AnalysisMetadata:
    """Metadata about a batch analysis run."""

    batch_id: str
    total_runs: int
    group_count: int
    partitions: list[list[str]]
    started_at: str
    finished_at: str | None
    conditions: list[str]
    tasks: list[str]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

# Regex to extract task name from run_id
_RUN_ID_RE = re.compile(
    r"^(.+)_(baseline|jarvis-prompted|orchestrated|jarvis-orchestrated)"
    r"_\d{8}-\d{6}_[a-f0-9]{4}$"
)


def _extract_task_name(run_id: str) -> str | None:
    """Extract the task name from a run ID."""
    m = _RUN_ID_RE.match(run_id)
    return m.group(1) if m else None


def _format_test_output(run_id: str, config: BenchConfig) -> str:
    """Load and format pytest output from test_results.json."""
    test_results_path = config.results_dir / run_id / "test_results.json"
    if not test_results_path.exists():
        return "(no test output available)"

    try:
        data = json.loads(test_results_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "(failed to read test output)"

    command_outputs = data.get("command_outputs", [])
    parts: list[str] = []
    for cmd_result in command_outputs:
        cmd = cmd_result.get("command", "unknown")
        exit_code = cmd_result.get("exit_code", "?")
        stdout = cmd_result.get("stdout", "")
        stderr = cmd_result.get("stderr", "")
        parts.append(f"$ {cmd}  (exit code: {exit_code})")
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        parts.append("")

    return "\n".join(parts)


def _format_workspace_summary(
    workspace_files: dict[str, str],
    test_output: str,
    max_total: int = 80_000,
    max_per_file: int = 5_000,
) -> str:
    """Format workspace files with priority ordering and truncation.

    Priority order:
    1. Files mentioned in pytest error tracebacks
    2. __init__.py files
    3. Common entry points (main.py, app.py)
    4. Remaining .py files
    5. Non-Python files last
    """
    # Find files mentioned in tracebacks
    traceback_files: set[str] = set()
    for line in test_output.split("\n"):
        # Match pytest traceback lines like "file.py:123: in func_name"
        m = re.search(r"(\S+\.py):\d+", line)
        if m:
            traceback_files.add(m.group(1))

    def _priority(path: str) -> tuple[int, str]:
        basename = path.rsplit("/", 1)[-1] if "/" in path else path
        # Check if any traceback file matches this path
        in_traceback = any(path.endswith(tf) or tf.endswith(basename) for tf in traceback_files)
        if in_traceback:
            return (0, path)
        if basename == "__init__.py":
            return (1, path)
        if basename in ("main.py", "app.py", "cli.py"):
            return (2, path)
        if path.endswith(".py"):
            return (3, path)
        return (4, path)

    sorted_files = sorted(workspace_files.keys(), key=_priority)

    parts: list[str] = []
    total_chars = 0
    for filepath in sorted_files:
        if total_chars >= max_total:
            parts.append(f"\n... ({len(sorted_files) - len(parts)} more files truncated)")
            break
        content = workspace_files[filepath]
        if len(content) > max_per_file:
            content = content[:max_per_file] + "\n... [truncated]"
        budget = max_total - total_chars
        if len(content) > budget:
            content = content[:budget] + "\n... [truncated]"
        parts.append(f"### {filepath}\n```\n{content}\n```")
        total_chars += len(content)

    return "\n\n".join(parts)


def prepare_run_context(
    run_id: str, config: BenchConfig
) -> RunAnalysisContext:
    """Load all data for a single run and build an analysis context."""
    grade = _load_grade_result(run_id, config)
    if grade is None:
        raise FileNotFoundError(f"No grades found for run {run_id}")

    # Test result data
    pass_rate = 0.0
    passed = failed = errors = total = 0
    if grade.test_result is not None:
        pass_rate = grade.test_result.success_rate
        passed = grade.test_result.passed
        failed = grade.test_result.failed
        errors = grade.test_result.errors
        total = grade.test_result.total

    # Quality scores
    quality_scores: dict[str, float] | None = None
    quality_rationale = ""
    if grade.quality_scores is not None:
        quality_scores = {
            "architectural_coherence": grade.quality_scores.architectural_coherence,
            "code_quality": grade.quality_scores.code_quality,
            "completeness": grade.quality_scores.completeness,
            "overall": grade.quality_scores.overall,
        }
        quality_rationale = grade.quality_scores.rationale

    # Test output
    test_output = _format_test_output(run_id, config)

    # Task spec
    task_name = grade.task_name
    try:
        task_spec = load_task_spec(task_name, config)
        spec_content = task_spec.spec_content
    except FileNotFoundError:
        spec_content = "(spec not found)"

    # Workspace files
    workspace_path = config.workspace_dir / run_id
    if workspace_path.is_dir():
        workspace_files = _read_workspace_files(workspace_path)
        workspace_summary = _format_workspace_summary(workspace_files, test_output)
    else:
        workspace_summary = "(workspace not found)"

    # PLAN.md
    plan_path = workspace_path / "PLAN.md"
    plan_content = plan_path.read_text(encoding="utf-8") if plan_path.exists() else None

    return RunAnalysisContext(
        run_id=run_id,
        task_name=task_name,
        condition=grade.condition.value,
        pass_rate=pass_rate,
        passed=passed,
        failed=failed,
        errors=errors,
        total=total,
        quality_scores=quality_scores,
        quality_rationale=quality_rationale,
        test_output=test_output,
        spec_content=spec_content,
        workspace_summary=workspace_summary,
        plan_content=plan_content,
    )


def prepare_batch_contexts(
    batch_id: str, config: BenchConfig
) -> list[RunAnalysisContext]:
    """Load all run contexts for a batch."""
    manifest = _load_manifest(batch_id, config)
    if manifest is None:
        raise FileNotFoundError(f"No manifest found for batch {batch_id}")

    runs = manifest.get("results", manifest.get("runs", []))
    contexts: list[RunAnalysisContext] = []
    for run_entry in runs:
        run_id = run_entry.get("run_id", "")
        if not run_id:
            continue
        try:
            ctx = prepare_run_context(run_id, config)
            contexts.append(ctx)
        except Exception:
            logger.exception("Failed to prepare context for run %s", run_id)

    return contexts


# ---------------------------------------------------------------------------
# Context formatting
# ---------------------------------------------------------------------------


def format_tier1_prompt(ctx: RunAnalysisContext) -> str:
    """Build the formatted prompt for a tier-1 per-run analyst agent."""
    # Truncation budgets
    spec = ctx.spec_content[:15_000] if len(ctx.spec_content) > 15_000 else ctx.spec_content
    plan = ""
    if ctx.plan_content:
        plan = ctx.plan_content[:5_000] if len(ctx.plan_content) > 5_000 else ctx.plan_content

    test_out = ctx.test_output
    if len(test_out) > 20_000:
        # Keep the tail (usually has summary) and truncate middle
        lines = test_out.split("\n")
        head = "\n".join(lines[:50])
        tail = "\n".join(lines[-200:])
        test_out = head + "\n\n... [middle truncated] ...\n\n" + tail
        if len(test_out) > 20_000:
            test_out = test_out[:20_000]

    workspace = ctx.workspace_summary  # already truncated by _format_workspace_summary

    quality_str = "N/A"
    if ctx.quality_scores:
        quality_str = (
            f"arch={ctx.quality_scores['architectural_coherence']:.1f}, "
            f"quality={ctx.quality_scores['code_quality']:.1f}, "
            f"completeness={ctx.quality_scores['completeness']:.1f}, "
            f"overall={ctx.quality_scores['overall']:.1f}"
        )
        if ctx.quality_rationale:
            quality_str += f"\nRationale: {ctx.quality_rationale}"

    return f"""You are analyzing a single evaluation run from JaRVIS-Bench.

## Run: {ctx.run_id}
- Task: {ctx.task_name} | Condition: {ctx.condition}
- Pass Rate: {ctx.pass_rate:.1%} ({ctx.passed}/{ctx.total} passed, {ctx.failed} failed, {ctx.errors} errors)
- Quality: {quality_str}

## Task Specification
<spec>
{spec}
</spec>

## Agent's Plan
<plan>
{plan or "No PLAN.md"}
</plan>

## Generated Code
<workspace>
{workspace}
</workspace>

## Test Output
<test_output>
{test_out}
</test_output>

## Produce this analysis (be thorough, take your time):

### Test Results Summary
Categorize each failed/errored test: what it tested, why it failed.

### Failure Root Causes
For each failure mode, identify the root cause in the code. Name file, function, line-level issue.

### What Went Right
Correctly implemented spec parts and good design decisions.

### Spec Coverage Gaps
Unimplemented or incorrectly implemented spec requirements.

### Prompt Improvement Suggestions
Specific, actionable changes to the task prompt or agent prompt.

### Journaling/Reflection Suggestions
{"(jarvis-prompted only) Did reflection help? What should it capture that it missed?" if "jarvis" in ctx.condition else "(baseline run — skip this section)"}
"""


def format_tier2_prompt(run_reports: dict[str, str]) -> str:
    """Build the formatted prompt for a tier-2 summarizer agent."""
    runs_xml = ""
    for run_id, report in run_reports.items():
        runs_xml += f'\n<run id="{run_id}">\n{report}\n</run>\n'

    return f"""You are summarizing {len(run_reports)} JaRVIS-Bench per-run analysis reports. Read each report
carefully and identify cross-cutting patterns.

{runs_xml}

## Produce:
### Common Failure Patterns (ranked by frequency)
### Baseline vs JaRVIS Comparison
### Task Difficulty Clusters
### Top 5 Actionable Recommendations
### Prompt Engineering Insights

Be thorough. Take your time to identify non-obvious patterns.
"""


def format_group_lead_prompt(
    batch_id: str,
    group_index: int,
    run_ids: list[str],
    config: BenchConfig,
) -> str:
    """Build the prompt for a group-lead agent that dispatches its own tier-1 analysts."""
    analysis_dir = config.analysis_dir / batch_id
    context_lines = "\n".join(
        f"- `{analysis_dir / 'contexts' / (rid + '.txt')}`"
        for rid in run_ids
    )
    runs_dir = analysis_dir / "runs"
    summaries_dir = analysis_dir / "summaries"

    return f"""You are a group-lead analyst for JaRVIS-Bench batch analysis.

## Your Group: Group {group_index} ({len(run_ids)} runs)
Runs: {', '.join(run_ids)}

## Instructions

1. Read each context file listed below (use the Read tool, all in parallel).
2. For EACH run, dispatch an Agent (subagent_type: general-purpose) with:
   - description: "Analyze run <run_id>"  (use the short run_id)
   - prompt: Prepend the following instruction before the full context file content:
     "Analyze this evaluation run and write a thorough report. Return your full analysis as markdown."
   - Dispatch ALL agents in a SINGLE message for maximum parallelism.
3. Collect each agent's analysis report.
4. Write each tier-1 report to: `{runs_dir}/<run_id>.md` (use the Write tool).
5. Synthesize a group summary comparing all runs in your group, covering:
   - **Common Failure Patterns** (ranked by frequency)
   - **Baseline vs JaRVIS Comparison**
   - **Task Difficulty Clusters**
   - **Top 5 Actionable Recommendations**
   - **Prompt Engineering Insights**
6. Write the summary to: `{summaries_dir}/summary_{group_index}.md`
7. Return the summary text as your final response.

## Context Files
{context_lines}
"""


# ---------------------------------------------------------------------------
# Auto-batching
# ---------------------------------------------------------------------------


def compute_tier2_partitions(
    run_ids: list[str], max_per_group: int = 8
) -> list[list[str]]:
    """Compute group partitions. Always returns at least one group.

    Groups by task name so baseline + jarvis for the same task stay together.
    """
    if not run_ids:
        return []

    # Group by task name
    task_groups: dict[str, list[str]] = {}
    ungrouped: list[str] = []
    for run_id in run_ids:
        task_name = _extract_task_name(run_id)
        if task_name:
            task_groups.setdefault(task_name, []).append(run_id)
        else:
            ungrouped.append(run_id)

    # Build partitions by packing task groups into chunks
    partitions: list[list[str]] = []
    current: list[str] = []
    for task_name in sorted(task_groups):
        group = task_groups[task_name]
        if len(current) + len(group) > max_per_group and current:
            partitions.append(current)
            current = []
        current.extend(group)
    if ungrouped:
        current.extend(ungrouped)
    if current:
        partitions.append(current)

    return partitions


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _analysis_base(batch_id: str, config: BenchConfig) -> Path:
    """Return the analysis directory for a batch, creating it if needed."""
    base = config.analysis_dir / batch_id
    base.mkdir(parents=True, exist_ok=True)
    return base


def save_context_file(
    batch_id: str, run_id: str, prompt: str, config: BenchConfig
) -> Path:
    """Save a prepared tier-1 context prompt to disk."""
    ctx_dir = _analysis_base(batch_id, config) / "contexts"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    path = ctx_dir / f"{run_id}.txt"
    path.write_text(prompt, encoding="utf-8")
    return path


def save_tier1_report(
    batch_id: str, run_id: str, report: str, config: BenchConfig
) -> Path:
    """Save a tier-1 per-run analysis report."""
    runs_dir = _analysis_base(batch_id, config) / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{run_id}.md"
    path.write_text(report, encoding="utf-8")
    return path


def save_tier2_summary(
    batch_id: str, index: int, summary: str, config: BenchConfig
) -> Path:
    """Save a tier-2 group summary."""
    summaries_dir = _analysis_base(batch_id, config) / "summaries"
    summaries_dir.mkdir(parents=True, exist_ok=True)
    path = summaries_dir / f"summary_{index}.md"
    path.write_text(summary, encoding="utf-8")
    return path


def save_final_report(
    batch_id: str, report: str, config: BenchConfig
) -> Path:
    """Save the final tier-3 synthesis report."""
    path = _analysis_base(batch_id, config) / "report.md"
    path.write_text(report, encoding="utf-8")
    return path


def save_metadata(
    batch_id: str, metadata: AnalysisMetadata, config: BenchConfig
) -> Path:
    """Save analysis metadata to JSON."""
    path = _analysis_base(batch_id, config) / "metadata.json"
    path.write_text(
        json.dumps(asdict(metadata), indent=2), encoding="utf-8"
    )
    return path


def load_tier1_reports(
    batch_id: str, config: BenchConfig
) -> dict[str, str]:
    """Load all tier-1 per-run reports from disk."""
    runs_dir = config.analysis_dir / batch_id / "runs"
    if not runs_dir.is_dir():
        return {}
    reports: dict[str, str] = {}
    for path in sorted(runs_dir.glob("*.md")):
        run_id = path.stem
        reports[run_id] = path.read_text(encoding="utf-8")
    return reports


def load_tier2_summaries(
    batch_id: str, config: BenchConfig
) -> list[str]:
    """Load all tier-2 group summaries from disk."""
    summaries_dir = config.analysis_dir / batch_id / "summaries"
    if not summaries_dir.is_dir():
        return []
    summaries: list[str] = []
    for path in sorted(summaries_dir.glob("*.md")):
        summaries.append(path.read_text(encoding="utf-8"))
    return summaries
