# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A/B evaluation framework comparing vanilla Claude Code vs Claude Code with JaRVIS reflection skills on NL2Repo-Bench's 104 Python library generation tasks. The hypothesis: reflective journaling helps agents maintain coherence on long-horizon coding tasks.

## Commands

```bash
# Initial setup (clones NL2RepoBench + JaRVIS into vendor/, installs deps)
./scripts/setup.sh
source .venv/bin/activate

# Install/reinstall the package
uv pip install -e .

# Lint
ruff check harness/

# Type check
mypy harness/

# Run evaluations (not yet implemented — see scaffold.md Phase 5)
./scripts/run-eval.sh --smoke              # Quick smoke test
./scripts/run-eval.sh --task <name>        # Single task
./scripts/run-eval.sh --full               # Full benchmark

# Python module entry points
python -m harness.runner --task <name> --condition jarvis
python -m harness.grader --run-id <id>
python -m harness.reporter --batch-id <id>

# Prepare analysis contexts for a batch (data prep only)
python -m harness analyze --batch-id <batch_id>

# Run full hierarchical analysis (interactive skill)
# /analyze-batch <batch_id>
```

## Architecture

The project has two layers: a Python evaluation engine (`harness/`) and shell entry points (`scripts/`).

**harness/** — Python package with four modules forming a pipeline:
- `config.py` → `runner.py` → `grader.py` → `reporter.py`
- **runner** orchestrates Claude Code sessions. For baseline: single autonomous run. For JaRVIS: installs skills into workspace, optionally generates PLAN.md first, triggers `/jarvis-reflect` after each step.
- **grader** evaluates completed workspaces two ways: (1) Docker-based pytest using NL2Repo-Bench's evaluation logic (primary metric), and (2) LLM-as-judge scoring via Anthropic API (supplementary).
- **reporter** aggregates graded results into per-task comparison tables and statistical summaries.

**Evaluation flow**: Task spec → Claude Code generates repo in workspace → Docker container runs upstream pytest suite → parse pass/fail/error → compute pass rate.

**Vendored deps** (gitignored, created by `scripts/setup.sh`):
- `vendor/NL2RepoBench/` — task specs in `test_files/{project}/start.md`, evaluation config in `test_commands.json`. Never modify.
- `vendor/JaRVIS/` — skills copied into workspaces for the treatment condition.

## Dev Conventions

- Python 3.11+, managed with `uv` (venv in `.venv/`)
- NL2Repo-Bench is vendored unmodified — never patch it directly
- Test pass rate is the primary metric; LLM judge scores are supplementary
- `results/` and `workspaces/` are gitignored (ephemeral per-run data)
- See `scaffold.md` for the full phased build plan — the harness modules are currently stubs (Phase 1 complete, Phases 2-6 pending)

## JaRVIS

Identity and memories are loaded automatically at session start via the SessionStart hook.
A Stop hook will remind you to reflect before ending a session if you haven't already.
Use `/jarvis-reload` to reload context mid-session if needed.
After completing any meaningful task, run `/jarvis-reflect` to capture what you learned.
You MUST run `/jarvis-reflect` before ending any session — do not end without reflecting.
Use `/jarvis-validate` to check `.jarvis/` format health.
Use `/jarvis-search` to find past entries by keyword, tag, date, or section.
