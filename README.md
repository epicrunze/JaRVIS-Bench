# JaRVIS-Bench

A/B evaluation framework measuring whether [JaRVIS](https://github.com/epicrunze/JaRVIS) reflective journaling improves [Claude Code](https://claude.ai/code)'s performance on long-horizon repository generation tasks.

**Hypothesis:** Reflective journaling helps AI coding agents maintain coherence on long-horizon tasks — planning better, catching mistakes earlier, and producing higher-quality code.

Uses [NL2Repo-Bench](https://github.com/multimodal-art-projection/NL2RepoBench) (104 Python library generation tasks) as the task and evaluation infrastructure.

## Prerequisites

- **Claude Code CLI** — installed and authenticated (`claude` on PATH)
- **Docker** — running (used for pytest evaluation of generated repos)
- **Python 3.11+** — managed with `uv`
- **Anthropic API key** — set as `ANTHROPIC_API_KEY` (used for LLM-as-judge scoring)

## Quick Start

```bash
# 1. Clone and set up
git clone https://github.com/epicrunze/JaRVIS-Bench.git
cd JaRVIS-Bench
./scripts/setup.sh          # Clones NL2RepoBench + JaRVIS into vendor/, installs deps
source .venv/bin/activate

# 2. Run a smoke test (1 easy task, 1 run per condition)
./scripts/run-eval.sh --smoke
```

## Running Evaluations

### Shell entry point

```bash
# Smoke test — single easy task, 1 run
./scripts/run-eval.sh --smoke

# Single task — 3 runs per condition (baseline + jarvis-prompted)
./scripts/run-eval.sh --task <task_name>

# Full benchmark — all 104 tasks
./scripts/run-eval.sh --full

# Task subset from file
./scripts/select-tasks.sh --easy --sample 10 > my_tasks.txt
./scripts/run-eval.sh --tasks-from my_tasks.txt

# Options
./scripts/run-eval.sh --task <name> --condition baseline --runs 5 --timeout 1800

# Grade or report on an existing batch
./scripts/run-eval.sh --grade-only <batch_id>
./scripts/run-eval.sh --report-only <batch_id>
```

### Python CLI (`jarvis-bench`)

```bash
# Run
jarvis-bench run --smoke
jarvis-bench run --task <name> --condition jarvis-prompted --runs 5
jarvis-bench run --full --timeout 1800
jarvis-bench run --tasks-from my_tasks.txt

# Grade
jarvis-bench grade --run-id <run_id>
jarvis-bench grade --batch-id <batch_id>

# Report
jarvis-bench report --batch-id <batch_id>
```

All subcommands accept `--project-root` (defaults to cwd) and `-v` for debug logging.

### Selecting task subsets

```bash
./scripts/select-tasks.sh --easy           # ≤50 test cases
./scripts/select-tasks.sh --medium         # 51–299 test cases
./scripts/select-tasks.sh --hard           # ≥300 test cases
./scripts/select-tasks.sh --all            # All 104 tasks
./scripts/select-tasks.sh --hard --sample 5  # Random 5 hard tasks
```

Pipe into `run-eval.sh`:

```bash
./scripts/select-tasks.sh --easy --sample 3 > tasks.txt
./scripts/run-eval.sh --tasks-from tasks.txt
```

## Interpreting Results

After a batch completes and is graded, generate a report:

```bash
jarvis-bench report --batch-id <batch_id>
# Report written to results/<batch_id>/report.md
```

The report contains:

- **Aggregate results** — mean pass rate and quality score per condition, with standard deviations
- **Win/Tie/Loss** — per-task comparison (JaRVIS vs baseline). A task is a "win" if JaRVIS's mean pass rate exceeds baseline by >1%, "loss" if below by >1%, "tie" otherwise
- **Per-task breakdown** — pass rate and quality score for each task under each condition
- **Improvement analysis** — top 5 most improved and most regressed tasks

**Primary metric:** Test pass rate (passed / total tests, via Docker pytest)
**Supplementary metric:** LLM-as-judge quality scores (architectural coherence, code quality, completeness on 0–10 scale)

## Project Structure

```
JaRVIS-Bench/
├── harness/                    # Python evaluation engine
│   ├── __init__.py
│   ├── __main__.py             # Click CLI (jarvis-bench command)
│   ├── config.py               # Dataclasses, enums, config, task discovery
│   ├── runner.py               # Claude Code invocation, workspace setup
│   ├── grader.py               # Docker pytest + LLM-as-judge scoring
│   └── reporter.py             # Aggregation, statistics, markdown reports
├── scripts/
│   ├── setup.sh                # One-time setup (clone deps, create venv)
│   ├── run-eval.sh             # Shell wrapper for python -m harness
│   └── select-tasks.sh         # Task subset selection by difficulty
├── docs/
│   ├── methodology.md          # A/B design, metrics, limitations
│   └── nl2repo-integration.md  # How we interface with NL2RepoBench
├── vendor/                     # Gitignored, created by setup.sh
│   ├── NL2RepoBench/           # Task specs + evaluation infra
│   └── JaRVIS/                 # Reflection skills (copied into workspaces)
├── workspaces/                 # Gitignored — per-run Claude Code workspaces
├── results/                    # Gitignored — run results, grades, reports
├── scaffold.md                 # Phased build plan
├── pyproject.toml
└── CLAUDE.md
```

## Extending

### Adding new tasks

Tasks come from NL2Repo-Bench. Each task lives in `vendor/NL2RepoBench/test_files/{name}/` with:

| File | Purpose |
|------|---------|
| `start.md` | Natural-language specification given to the coding agent |
| `test_commands.json` | JSON array of shell commands (typically pytest invocations) |
| `test_files.json` | Test file references used during evaluation |
| `test_case_count.txt` | Expected number of test cases |

To add custom tasks, create a directory following this structure. The grader also needs a Docker base image — see `docs/nl2repo-integration.md` for details.

### Customizing evaluation

- **Docker grading** (`harness/grader.py`): Stages workspace, removes package/test files, builds Docker image, runs test commands, parses pytest output
- **LLM judge** (`harness/grader.py`): Reads workspace files, sends to Claude Sonnet for scoring on three dimensions
- **Reporter** (`harness/reporter.py`): Aggregates grades, computes win/tie/loss, renders markdown

See `docs/methodology.md` for full details on metrics and statistical approach.

## Development

```bash
source .venv/bin/activate
uv pip install -e ".[dev]"
ruff check harness/
mypy harness/
```
