# Methodology

How JaRVIS-Bench evaluates whether reflective journaling improves AI coding agent performance.

## A/B Design

Each NL2Repo-Bench task is run under two primary conditions:

| | Baseline | JaRVIS-Prompted |
|---|---|---|
| **Agent** | Claude Code | Claude Code |
| **Model** | Different (default: claude-sonnet-4-6) | Different |
| **Task spec** | Same `start.md` | Same `start.md` |
| **Timeout** | Same (default: 1200s) | Same |
| **JaRVIS skills** | No | Yes — copied into `.claude/skills/` |
| **PLAN.md** | No | Yes — agent prompted to create one before coding |
| **Reflection** | No | Yes — agent prompted to use `/jarvis-reflect` after major components |
| **CLAUDE.md** | No | Yes — workspace gets JaRVIS CLAUDE.md with reflection hooks |

The key independent variable is whether the agent has access to JaRVIS reflection infrastructure (skills, journal scaffolding, reflection prompts). Everything else is controlled.

## Conditions

The `Condition` enum defines four experimental conditions:

### Primary conditions

- **`baseline`** — Vanilla Claude Code. The agent receives the task specification and works autonomously in a single session. No planning prompt, no reflection, no JaRVIS skills.

- **`jarvis-prompted`** — Claude Code with JaRVIS skills installed and prompt-driven reflection. The agent is instructed to create `PLAN.md` before coding and to call `/jarvis-reflect` after each major component (targeting 3–5 reflections per task). The workspace includes `.jarvis/` scaffolding (identity, growth log, memories) and a `CLAUDE.md` with reflection hooks.

### Future conditions (not yet implemented)

- **`orchestrated`** — Multi-step orchestration without JaRVIS. The harness breaks the task into steps and invokes Claude Code sequentially with checkpoints between steps.

- **`jarvis-orchestrated`** — Multi-step orchestration with JaRVIS. Same step-by-step approach but with reflection between steps.

The default configuration runs `baseline` and `jarvis-prompted` only.

## Reflection Boundaries

In the `jarvis-prompted` condition, reflection is prompt-driven:

1. The prompt instructs the agent to create `PLAN.md` first
2. The prompt asks for `/jarvis-reflect` after each major component
3. The agent decides when "major component" boundaries occur
4. JaRVIS skills are available but invocation is voluntary

This means the number and timing of reflections varies per run — it depends on how the agent interprets "major component." This is intentional: we're testing whether the reflection *capability* helps, not a fixed reflection schedule.

## Evaluation Pipeline

```
Task spec (start.md)
    │
    ▼
Claude Code generates repo in workspace/
    │
    ▼
Grader stages workspace (removes package files + test files)
    │
    ▼
Docker container runs upstream pytest suite
    │
    ▼
Parse pass/fail/error counts from pytest output
    │
    ▼
LLM-as-judge scores workspace (supplementary)
    │
    ▼
Reporter aggregates across runs → report.md
```

### Workspace staging

Before Docker evaluation, the grader copies the workspace to a staging directory and removes:

- **Package management files** (`setup.py`, `pyproject.toml`, `requirements.txt`, etc.) — prevents conflicts with the upstream test environment's package configuration
- **Test files/directories** listed in `test_files.json` — the upstream test suite is already in the Docker base image; removing agent-generated tests prevents conflicts

### Docker evaluation

1. Build a Docker image: NL2Repo-Bench base image + staged workspace contents
2. Start a container from that image
3. Execute each command from `test_commands.json` (typically pytest invocations)
4. Parse pytest output for pass/fail/error counts
5. Clean up container and image

### LLM-as-judge

A separate Claude Sonnet call evaluates the workspace on three dimensions (see Supplementary Metric below). This runs independently of Docker evaluation — either can fail without affecting the other.

## Primary Metric: Test Pass Rate

```
pass_rate = passed / total
```

Where `total` comes from `test_case_count.txt` if available, otherwise `total = passed + failed + errors`. The pass rate is capped at 1.0.

This is the ground-truth metric: does the generated code actually work? It uses the upstream NL2Repo-Bench test suite, so we're measuring against the same standard used in the original benchmark.

## Supplementary Metric: LLM Judge Scores

Claude Sonnet (`claude-sonnet-4-6`) evaluates each workspace against the original specification on three dimensions, each scored 0–10:

- **Architectural coherence** — How well-organized is the code? Are modules logically separated? Does the structure match the spec's requirements?
- **Code quality** — Is the code clean, idiomatic Python? Proper error handling, naming, typing?
- **Completeness** — How much of the specification is actually implemented? Are all required features present?

The **overall** score is the mean of all three dimensions.

These scores are supplementary — they capture aspects that tests alone miss (e.g., an agent might pass tests with poorly structured code). However, they are subject to judge model bias and should not be treated as primary evidence.

## Statistical Approach

### Multiple runs

Each task × condition combination is run multiple times (default: 3). This captures LLM nondeterminism — the same prompt can produce different code on different runs.

### Aggregation

- **Per-task:** Mean and population standard deviation of pass rates and quality scores across runs for each condition
- **Overall:** Grand mean and population standard deviation across all runs per condition

Population standard deviation (not sample) is used since we're computing descriptive statistics over all runs, not estimating population parameters.

### Win/Tie/Loss

For each task, compare the mean pass rate of the best non-baseline condition against baseline:

- **Win:** Non-baseline mean pass rate exceeds baseline by >1% (absolute)
- **Loss:** Non-baseline mean pass rate is below baseline by >1% (absolute)
- **Tie:** Difference is within ±1%

The 1% threshold avoids counting noise as signal. With 3 runs per condition and stochastic LLM output, small differences are not meaningful.

## Known Limitations

### LLM nondeterminism
Claude Code's output varies across runs even with identical inputs. Three runs per condition captures some of this variance but may not be sufficient for borderline cases. Increasing `--runs` improves statistical power at the cost of time and money.

### Cost
Each Claude Code session costs roughly $5–20+ depending on task complexity and model. A full benchmark with default settings (104 tasks × 2 conditions × 3 runs = 624 sessions) costs in the range of $3,000–$12,000+. The smoke test (`--smoke`) runs a single easy task with 1 run to verify the pipeline cheaply.

### Planning as confound
The `jarvis-prompted` condition includes a prompt to create `PLAN.md` before coding. This means we're testing "planning + reflection" vs "neither" — we can't attribute improvements to reflection alone. The `orchestrated` and `jarvis-orchestrated` conditions (not yet implemented) would help isolate this.

### Judge model bias
Using Claude Sonnet to judge Claude Code's output creates a potential same-family bias. The judge may favor code patterns that Claude models tend to produce. Quality scores should be interpreted alongside test pass rates, not in isolation.

### Docker evaluation fidelity
Some tasks may have test suites that are sensitive to environment details (Python version, installed packages, file system layout). The Docker base images from NL2Repo-Bench are designed to handle this, but edge cases exist.

## Task Difficulty Tiers

Tasks are categorized by test case count:

| Tier | Test cases | Hypothesis |
|------|-----------|------------|
| Easy | ≤50 | Baseline may already handle these well; less room for reflection to help |
| Medium | 51–299 | Moderate complexity; reflection may help with planning and coherence |
| Hard | ≥300 | Long-horizon tasks where maintaining coherence is critical; strongest expected benefit from reflection |

Use `scripts/select-tasks.sh` to filter by difficulty:

```bash
./scripts/select-tasks.sh --easy    # List easy tasks
./scripts/select-tasks.sh --hard    # List hard tasks
```

The smoke test uses `graphneuralnetwork` (4 test cases) — an easy task for quick pipeline validation.
