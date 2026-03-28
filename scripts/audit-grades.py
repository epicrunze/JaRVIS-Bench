#!/usr/bin/env python3
"""Audit existing grade results for known grading accuracy issues.

Usage: uv run python scripts/audit-grades.py <batch_id>

Checks for:
  1. Parser inflation (passed > total)
  2. Pytest command timeouts
  3. pip install failures
  4. test_case_count.txt vs actual collected count mismatches
  5. Skipped/xfail tests in output but not tracked
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AuditIssue:
    run_id: str
    task_name: str
    condition: str
    issue_type: str
    details: str


@dataclass
class AuditReport:
    issues: list[AuditIssue] = field(default_factory=list)
    parser_inflation: list[AuditIssue] = field(default_factory=list)
    command_timeouts: list[AuditIssue] = field(default_factory=list)
    pip_failures: list[AuditIssue] = field(default_factory=list)
    collected_mismatches: list[AuditIssue] = field(default_factory=list)
    untracked_skipped: list[AuditIssue] = field(default_factory=list)


def _parse_collected_from_output(output: str) -> int:
    m = re.search(r"collected (\d+) items?", output)
    return int(m.group(1)) if m else 0


def _count_in_summary(output: str, keyword: str) -> int:
    """Count occurrences of keyword in pytest summary lines."""
    total = 0
    for line in output.split("\n"):
        stripped = line.strip()
        if re.match(r"^=+\s.*\s=+$", stripped):
            m = re.search(rf"(\d+) {keyword}", stripped)
            if m:
                total += int(m.group(1))
    return total


def audit_batch(batch_id: str, project_root: Path) -> AuditReport:
    manifest_path = project_root / "results" / batch_id / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest not found at {manifest_path}", file=sys.stderr)
        sys.exit(1)

    with open(manifest_path) as f:
        manifest = json.load(f)

    report = AuditReport()

    for run in manifest.get("runs", []):
        run_id = run["run_id"]
        task_name = run["task_name"]
        condition = run["condition"]

        grades_path = project_root / "results" / run_id / "grades.json"
        if not grades_path.exists():
            continue

        with open(grades_path) as f:
            grades = json.load(f)

        tr = grades.get("test_result")
        if not tr:
            continue

        passed = tr.get("passed", 0)
        total = tr.get("total", 0)
        command_outputs = tr.get("command_outputs", [])

        # 1. Parser inflation: passed > total
        if total > 0 and passed > total:
            issue = AuditIssue(
                run_id=run_id,
                task_name=task_name,
                condition=condition,
                issue_type="parser_inflation",
                details=f"passed={passed} > total={total} (ratio: {passed/total:.1f}x)",
            )
            report.parser_inflation.append(issue)
            report.issues.append(issue)

        # 2. Pytest command timeouts
        for cmd_result in command_outputs:
            if cmd_result.get("timed_out"):
                # Estimate progress from output
                stdout = cmd_result.get("stdout", "")
                progress = "unknown"
                m = re.search(r"\[.*?(\d+)%\]", stdout)
                if m:
                    progress = f"{m.group(1)}%"
                issue = AuditIssue(
                    run_id=run_id,
                    task_name=task_name,
                    condition=condition,
                    issue_type="command_timeout",
                    details=f"command timed out, progress: {progress}",
                )
                report.command_timeouts.append(issue)
                report.issues.append(issue)
                break  # one per run

        # 3. pip install failures
        for cmd_result in command_outputs:
            cmd = cmd_result.get("command", "")
            if "pip" in cmd.lower() and "pytest" not in cmd.lower():
                if cmd_result.get("exit_code", 0) != 0:
                    issue = AuditIssue(
                        run_id=run_id,
                        task_name=task_name,
                        condition=condition,
                        issue_type="pip_failure",
                        details=f"pip install exit_code={cmd_result.get('exit_code')}",
                    )
                    report.pip_failures.append(issue)
                    report.issues.append(issue)
                    break

        # 4-5. Check pytest output for collected count and skipped/xfail
        for cmd_result in command_outputs:
            cmd = cmd_result.get("command", "")
            if "pytest" not in cmd.lower():
                continue

            stdout = cmd_result.get("stdout", "") or ""
            stderr = cmd_result.get("stderr", "") or ""
            output = stdout + "\n" + stderr

            # Collected count mismatch
            collected = _parse_collected_from_output(output)
            test_case_count_path = (
                project_root / "vendor" / "NL2RepoBench" / "test_files"
                / task_name / "test_case_count.txt"
            )
            expected_total = 0
            if test_case_count_path.exists():
                text = test_case_count_path.read_text().strip()
                if text:
                    expected_total = int(text)

            if collected > 0 and expected_total > 0 and collected != expected_total:
                issue = AuditIssue(
                    run_id=run_id,
                    task_name=task_name,
                    condition=condition,
                    issue_type="collected_mismatch",
                    details=f"collected={collected} vs test_case_count={expected_total} (diff: {collected - expected_total:+d})",
                )
                report.collected_mismatches.append(issue)
                report.issues.append(issue)

            # Untracked skipped/xfail
            skipped_count = _count_in_summary(output, "skipped")
            xfail_count = _count_in_summary(output, "xfailed")
            if skipped_count > 0 or xfail_count > 0:
                # Check if already tracked in grades
                if not tr.get("skipped") and not tr.get("xfailed"):
                    issue = AuditIssue(
                        run_id=run_id,
                        task_name=task_name,
                        condition=condition,
                        issue_type="untracked_skipped",
                        details=f"skipped={skipped_count}, xfailed={xfail_count} (not in grades)",
                    )
                    report.untracked_skipped.append(issue)
                    report.issues.append(issue)
            break  # only check first pytest command

    return report


def print_report(report: AuditReport) -> None:
    def _section(title: str, issues: list[AuditIssue]) -> None:
        print(f"\n{'='*60}")
        print(f"{title} ({len(issues)} runs affected)")
        print(f"{'='*60}")
        if not issues:
            print("  (none)")
            return

        # Group by condition
        by_condition: dict[str, int] = defaultdict(int)
        by_task: dict[str, int] = defaultdict(int)
        for i in issues:
            by_condition[i.condition] += 1
            by_task[i.task_name] += 1

        print(f"  By condition: {dict(by_condition)}")
        print(f"  By task ({len(by_task)} tasks):")
        for task in sorted(by_task, key=lambda t: by_task[t], reverse=True)[:15]:
            print(f"    {task}: {by_task[task]}")

        print("\n  Details (first 20):")
        for i in issues[:20]:
            print(f"    {i.run_id}: {i.details}")
        if len(issues) > 20:
            print(f"    ... and {len(issues) - 20} more")

    print("\nGrading Audit Report")
    print(f"Total issues found: {len(report.issues)}")

    _section("1. Parser Inflation (passed > total)", report.parser_inflation)
    _section("2. Pytest Command Timeouts", report.command_timeouts)
    _section("3. pip Install Failures", report.pip_failures)
    _section("4. Collected Count Mismatches", report.collected_mismatches)
    _section("5. Untracked Skipped/Xfail", report.untracked_skipped)

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"  Parser inflation:      {len(report.parser_inflation)} runs")
    print(f"  Command timeouts:      {len(report.command_timeouts)} runs")
    print(f"  pip failures:          {len(report.pip_failures)} runs")
    print(f"  Collected mismatches:  {len(report.collected_mismatches)} runs")
    print(f"  Untracked skipped:     {len(report.untracked_skipped)} runs")
    print(f"  Total issues:          {len(report.issues)}")


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: uv run python scripts/audit-grades.py <batch_id>", file=sys.stderr)
        sys.exit(1)

    batch_id = sys.argv[1]
    project_root = Path(__file__).resolve().parent.parent

    print(f"Auditing grades for {batch_id}...")
    report = audit_batch(batch_id, project_root)
    print_report(report)


if __name__ == "__main__":
    main()
