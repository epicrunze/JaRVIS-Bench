# JaRVIS-Bench: Build Plan

This document is the build plan for JaRVIS-Bench, an A/B evaluation framework that measures whether JaRVIS reflective journaling improves Claude Code's coding performance on long-horizon repository generation tasks.

We use NL2Repo-Bench (https://github.com/multimodal-art-projection/NL2RepoBench) as our task and evaluation infrastructure, and add a thin runner layer that executes each task under two conditions: vanilla Claude Code (baseline) vs Claude Code with JaRVIS installed (treatment).

## Context

JaRVIS (https://github.com/epicrunze/JaRVIS) is a set of agent skills for Claude Code that give the agent persistent memory, post-task reflection, and a self-evolving identity via flat markdown files. The core hypothesis is:

> An agent that periodically reflects on its work mid-task will produce better code on long-horizon tasks than one that does not, because reflection helps maintain global coherence, avoid repeated mistakes, and sustain architectural consistency across hundreds of interaction steps.

NL2Repo-Bench provides 104 Python library generation tasks where an agent receives a natural-language spec and must build a complete, installable repo from scratch. Evaluation is execution-based: the generated code is tested against the original project's upstream pytest suite in a Docker container. The primary metric is test pass rate.

NL2Repo-Bench's documented failure modes — loss of global coherence, premature termination, fragile cross-file dependencies, and inadequate planning — are exactly the problems JaRVIS reflection is designed to mitigate.

---

## Phase 1: Repository Setup and NL2Repo-Bench Integration

### Step 1.1: Initialize the repo

Create the repo structure:

```
jarvis-bench/
├── CLAUDE.md                    # This file (build plan + project instructions)
├── README.md                    # Public-facing docs
├── pyproject.toml               # Python project config
├── harness/                     # The evaluation engine
│   ├── __init__.py
│   ├── config.py                # Configuration and defaults
│   ├── runner.py                # Orchestrates Claude Code runs
│   ├── grader.py                # Scores results (test pass rate + LLM judge)
│   └── reporter.py              # Generates comparison reports
├── scripts/
│   ├── setup.sh                 # Install deps, clone NL2Repo-Bench, verify prereqs
│   ├── run-eval.sh              # Main entry point
│   └── select-tasks.sh          # Helper to pick a subset of tasks
├── results/                     # Evaluation results (gitignored except .gitkeep)
│   └── .gitkeep
└── workspaces/                  # Ephemeral workspaces per run (gitignored)
    └── .gitkeep
```

### Step 1.2: Write `scripts/setup.sh`

This script should:

1. Check prerequisites: `claude` CLI installed and authenticated, `docker` available, Python 3.11+
2. Clone NL2Repo-Bench into `vendor/NL2RepoBench/` (or skip if already present)
3. Pull required Docker images for NL2Repo evaluation (see NL2RepoBench readme for image names: `docker.all-hands.dev/all-hands-ai/openhands:0.56` and the runtime image)
4. Clone JaRVIS into `vendor/JaRVIS/` (or skip if already present)
5. Install Python dependencies from pyproject.toml
6. Print a summary of what's ready and what's missing

### Step 1.3: Understand NL2Repo-Bench's evaluation pipeline

Before writing the runner, read through NL2RepoBench's code thoroughly:

- `main.py` — the main execution entry point
- `only_test.py` — the standalone test runner (this is what we need for evaluation)
- `test_data_service.py` — how test data is served to the evaluation containers
- `config.json` — how tasks are configured
- `test_files/` — the task specs and test configurations (each subdirectory is a task)
- `docker_self/` — Docker configuration for self-hosted evaluation

The key thing to extract is: **how does `only_test.py` take a completed workspace and run the upstream pytest suite against it inside a Docker container?** We need to call this same evaluation logic from our runner.

Document what you learn in a `docs/nl2repo-integration.md` file so we can reference it later.

---

## Phase 2: The Runner

### Step 2.1: Write `harness/config.py`

Configuration dataclass with:

- Paths: project root, vendor dirs, workspace dir, results dir
- Claude Code settings: `claude` command path, per-task timeout (suggest 600s for these long tasks), output format
- JaRVIS settings: path to JaRVIS skills, whether to generate a plan before starting, reflection trigger (after each plan step)
- NL2Repo settings: path to NL2RepoBench, path to test_files, Docker image names
- Evaluation settings: number of runs per condition (default 3), which tasks to run (all, subset, or specific task names)

### Step 2.2: Write `harness/runner.py`

This is the core of the project. It runs a single NL2Repo task under one condition (baseline or jarvis).

**For BOTH conditions:**

1. Create a fresh workspace directory: `workspaces/<run-id>/<task-name>/`
2. Read the task's NL spec from `vendor/NL2RepoBench/test_files/<task-name>/`
3. Run Claude Code with the spec as the prompt, pointed at the workspace
4. Capture: Claude Code's full output, wall-clock time, exit code, token usage if available
5. List all files generated in the workspace

**For the BASELINE condition:**

- Run Claude Code with a simple system prompt: "You are building a Python library from this specification. Build the complete, installable repository in the current directory."
- Let Claude Code run autonomously until it finishes or hits the timeout

**For the JARVIS condition:**

- Pre-install JaRVIS skills into the workspace (copy from `vendor/JaRVIS/skills/` into `workspace/.claude/skills/`)
- Create a CLAUDE.md in the workspace with JaRVIS instructions
- **Step A — Plan generation**: Run Claude Code with the prompt: "Read the following specification and create a detailed step-by-step implementation plan. Save it as PLAN.md. Each step should be a discrete, completable unit of work. Do not start implementation yet."
- **Step B — For each step in PLAN.md**: Run Claude Code with the prompt: "Execute step N of PLAN.md: [step description]. Work in the current directory." After Claude Code finishes each step, run Claude Code again with: "/jarvis-reflect"
- **Step C — After every 5 reflections**: Run Claude Code with: "/jarvis-identity"

**Important implementation detail**: Each Claude Code invocation within the JaRVIS condition should run in the SAME workspace directory, so that code, `.jarvis/` artifacts, and `CLAUDE.md` all persist across steps. Use `claude -p "<prompt>" --output-format json` for non-interactive execution, or investigate `claude --continue` if available for maintaining session context.

**Alternative simpler approach** (if step-by-step execution is too complex or fragile): Run Claude Code once in the JaRVIS condition with a modified prompt that says: "Build this library following a step-by-step approach. After completing each major component, run /jarvis-reflect before proceeding to the next. Create a PLAN.md first." This lets Claude Code manage its own plan execution and reflection timing, which is more natural. Try this approach first, fall back to the step-by-step orchestration if Claude Code doesn't reliably reflect on its own.

### Step 2.3: Write the orchestration layer

A function `run_evaluation(task_name, condition, config)` that:

1. Calls the runner for the given task/condition
2. Saves all raw outputs to `results/<run-id>/raw/`
3. Returns a structured result object

A function `run_full_benchmark(task_names, conditions, num_runs, config)` that:

1. For each task × condition × run: call `run_evaluation`
2. Handle parallelism if desired (but be careful with Claude Code rate limits)
3. Save a manifest of all runs to `results/<batch-id>/manifest.json`

---

## Phase 3: The Evaluator

### Step 3.1: Write `harness/grader.py`

**Correctness grading (automated, primary metric):**

Reuse NL2Repo-Bench's Docker-based evaluation. For each completed workspace:

1. Package the workspace the way NL2Repo expects it
2. Mount it into the evaluation Docker container
3. Run the upstream pytest suite
4. Parse results: total tests, passed, failed, errors
5. Compute test pass rate: `passed / total`

Study `only_test.py` and `test_data_service.py` carefully to understand the exact Docker invocation and result parsing. Wrap their evaluation logic in a Python function we can call programmatically.

**Quality grading (LLM-as-judge, secondary metric):**

For each completed workspace, use the Anthropic API to score the generated code on:

- **Architectural coherence** (0-10): Does the code have a sensible project structure? Are modules well-organized? Are cross-file dependencies clean?
- **Code quality** (0-10): Is the code idiomatic, readable, well-documented?
- **Completeness** (0-10): Does the implementation cover all aspects of the spec?

The judge prompt should receive: the original NL spec, a listing of all generated files with their contents, and a rubric. Use `claude-sonnet-4-20250514` as the judge model. Request JSON output for reliable parsing.

**Important**: The correctness score (test pass rate) is the primary metric. The LLM judge scores are supplementary and should be treated as noisy signals.

### Step 3.2: Integrate grading into the pipeline

After a run completes, automatically grade it:

1. Run the Docker-based test evaluation → save to `results/<run-id>/test_results.json`
2. Run the LLM judge → save to `results/<run-id>/quality_scores.json`
3. Combine into `results/<run-id>/grades.json`

---

## Phase 4: The Reporter

### Step 4.1: Write `harness/reporter.py`

Given a batch of graded runs (multiple tasks × 2 conditions × N repetitions), generate:

**Per-task comparison table:**

| Task | Baseline Pass Rate | JaRVIS Pass Rate | Delta | Baseline Quality | JaRVIS Quality | Delta |
|------|-------------------|-----------------|-------|-----------------|---------------|-------|
| math-verify | 0.35 | 0.42 | +0.07 | 6.2 | 7.1 | +0.9 |
| ... | | | | | | |

**Aggregate statistics:**

- Mean test pass rate per condition (with std dev across runs)
- Mean quality scores per condition
- Number of tasks where JaRVIS outperformed / tied / underperformed baseline
- Paired comparison: for each task, did JaRVIS consistently beat baseline across runs?

**Improvement analysis:**

- Which NL2Repo task categories (system tools, data processing, ML, networking, etc.) benefit most from JaRVIS?
- Correlation between task difficulty (easy/medium/hard per NL2Repo's classification) and JaRVIS benefit — hypothesis: harder tasks benefit more because they have more opportunities for coherence loss
- If step-by-step execution was used: how many reflections occurred per task? Is there a correlation between reflection count and improvement?

Output as a markdown report: `results/<batch-id>/report.md`

---

## Phase 5: CLI and Entry Points

### Step 5.1: Write `scripts/run-eval.sh`

Main entry point that wraps the Python harness:

```bash
# Run full benchmark (all 104 tasks, both conditions, 3 runs each)
./scripts/run-eval.sh --full

# Run a specific task
./scripts/run-eval.sh --task math-verify --condition both --runs 3

# Run a quick smoke test (1 easy task, 1 run each)
./scripts/run-eval.sh --smoke

# Run only grading on existing results
./scripts/run-eval.sh --grade-only --batch-id <id>

# Run only the report on existing grades
./scripts/run-eval.sh --report-only --batch-id <id>
```

### Step 5.2: Write `scripts/select-tasks.sh`

Helper to pick task subsets from NL2Repo's 104 tasks:

- `--easy` — only easy tasks (quick iteration)
- `--hard` — only hard tasks (where JaRVIS should help most)
- `--sample N` — random N tasks
- `--category <name>` — tasks from a specific category

### Step 5.3: Add CLI entry points to the Python package

Use `click` for the Python CLI:

```bash
python -m harness.runner --task math-verify --condition jarvis
python -m harness.grader --run-id <id>
python -m harness.reporter --batch-id <id>
```

---

## Phase 6: Documentation and Polish

### Step 6.1: Write README.md

Public-facing documentation covering:

- What this benchmark measures and why
- Prerequisites (Claude Code CLI, Docker, API key)
- Quick start (smoke test)
- Full benchmark instructions
- How to interpret results
- How to add new tasks or modify the evaluation

### Step 6.2: Write docs/methodology.md

Detailed methodology document covering:

- The A/B design: what's controlled, what varies
- How reflection boundaries are determined (plan steps)
- The evaluation pipeline: Docker pytest → LLM judge → report
- Known limitations and threats to validity (LLM nondeterminism, cost, the plan itself being a confound)
- Statistical considerations: how many runs are needed, how to interpret deltas

### Step 6.3: Write docs/nl2repo-integration.md

(Created in Phase 1) — technical details of how we interface with NL2Repo-Bench's evaluation infrastructure.

---

## Build Order

Execute the phases in this order. Each phase should be fully working before moving to the next.

1. **Phase 1**: Repo setup, clone deps, understand NL2Repo evaluation pipeline
2. **Phase 2**: Runner — get a single task running under both conditions end-to-end
3. **Phase 3**: Grader — get test pass rates computed for a completed run
4. **Phase 4**: Reporter — get a comparison report from graded runs
5. **Phase 5**: CLI wrappers and entry points
6. **Phase 6**: Documentation

**Milestone checkpoint after Phase 3**: At this point you should be able to run one easy NL2Repo task under both conditions, grade both, and see test pass rates. That's the minimal viable benchmark. Everything after is polish.

---

## Key Design Decisions

- **NL2Repo-Bench is not modified.** We use it as a vendored dependency. All our code is in `harness/` and `scripts/`. If NL2Repo updates, we can pull changes.
- **The plan document is part of the treatment.** In the JaRVIS condition, Claude Code generates PLAN.md before building. This is intentional — structured planning is part of the JaRVIS workflow. The baseline does NOT get a plan. If you want to control for planning separately, add a third condition: "plan but no reflection."
- **Test pass rate is the primary metric.** LLM judge scores are supplementary. Don't overweight them.
- **Start with the simple approach.** Try letting Claude Code manage its own reflection timing before building complex step-by-step orchestration. If the simple approach works, it's more realistic anyway (it mirrors how a real user would use JaRVIS).
- **Cost awareness.** Running 104 tasks × 2 conditions × 3 runs = 624 Claude Code sessions. Each long-horizon task could cost $5-20+ in API usage. Budget accordingly. Start with a small subset (5-10 easy tasks) to validate the pipeline before scaling up.

---

## Dependencies

- Python 3.11+
- Claude Code CLI (`claude`) — installed and authenticated
- Docker — for NL2Repo evaluation containers
- `anthropic` Python SDK — for LLM-as-judge grading
- `click` — CLI framework
- `pytest` — for running NL2Repo test suites (inside Docker)

## pyproject.toml

```toml
[project]
name = "jarvis-bench"
version = "0.1.0"
description = "A/B benchmark: does JaRVIS reflection improve Claude Code on long-horizon coding tasks?"
requires-python = ">=3.11"
license = "MIT"
dependencies = [
    "anthropic>=0.40.0",
    "click>=8.1",
]

[project.optional-dependencies]
dev = [
    "ruff>=0.5",
    "mypy>=1.10",
]
```