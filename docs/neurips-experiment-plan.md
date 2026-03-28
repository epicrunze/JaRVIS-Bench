# Plan: NeurIPS Experiment Implementation

## Context

We're expanding JaRVIS-Bench from a single Sonnet experiment to a multi-model evaluation for a NeurIPS paper. The central hypothesis is that reflection benefit scales with model capability. We need three new experiment types implemented in the harness, with proper logging so analysis can come later.

Sonnet batch (`batch_20260318-033128_1f0e`) is graded — 90 paired tasks, 37 JaRVIS wins / 25 ties / 28 baseline wins, +3.1% mean delta, Cohen's d = 0.200. 35 runs still need rerunning (17 errors + 18 idle timeouts). The focus here is harness implementation.

---

## Implementation Items

### 1. Curate Task Subset

Everything else depends on this — the subset is used for no-plan ablation, Opus, and Qwen experiments. Blocked on: (a) Sonnet batch reruns completing (35 runs: 17 errors + 18 idle timeouts), (b) re-grading of rerun results.

#### Selection Algorithm

Applied in this order:

1. **Exclude unpaired tasks** — drop any task without clean runs in BOTH conditions.
   Currently 6 tasks: schedule-master, tqdm, trimming (baseline-only); bleach, pandarallel, parse (JaRVIS-only).

2. **Exclude uninformative tasks** — drop tasks where both conditions saturate at ceiling (≥99% both) OR floor (≤2% both). These can't differentiate conditions.
   - Ceiling: aiofiles, autopep8, decouple, pytest-cov, python-pathspec
   - Floor: arxiv-mcp-server, boto, python-slugify, pytorch-grad-cam, graphneuralnetwork, python-pytest-cases
   - ~11 tasks removed.

3. **Require minimum confidence** — strongly prefer tasks with 3+ clean runs per condition (high confidence). Tasks with 2+ per condition (medium) are acceptable to fill tier gaps, but cap at ≤25% of the subset. With n=2 a single outlier flips the pass rate by 50pp — too noisy to be a primary selection signal. ~30 low-confidence tasks removed (some may become eligible after reruns complete).

4. **Compute difficulty tier** — for each remaining task, count code-block LOC in `start.md`. Apply calibrated thresholds:
   - Easy: ≤514 LOC
   - Medium: 515–1207 LOC
   - Hard: >1207 LOC

   These thresholds reproduce NL2Repo-Bench's 25%/44%/31% distribution. Note: the paper's difficulty labels (based on original repo LOC) aren't published per-task, so start.md LOC is our best available proxy.

   **Validation step:** before using these tiers, verify that the proxy tracks observed difficulty — compute rank correlation between start.md LOC and mean pass rate (across both conditions). If the correlation is weak (|r| < 0.3), consider a composite proxy that also incorporates test case count or average Sonnet pass rate. Report the correlation in the paper appendix regardless.

5. **Stratified selection** — from the eligible pool, select:
   - ~8–10 easy tasks
   - ~12–15 medium tasks
   - ~12–15 hard tasks
   - ~3–5 negative controls (Sonnet delta ≈ 0, i.e. tie tasks) spread across tiers

   Within each tier, prioritize:
   - High-confidence (3+3 runs) over medium-confidence (2+2)
   - **Pass-rate diversity** — spread selections across the pass-rate spectrum within the tier (don't cluster around only high or only low pass rates)
   - |Delta| magnitude as a **tiebreaker only**, not a primary selection criterion. Selecting primarily for large deltas would bias toward confirming the hypothesis on tasks cherry-picked from Sonnet results.

   The negative controls serve two purposes: (a) check that the cross-model experiment doesn't find spurious effects on tasks where Sonnet showed none, and (b) detect cases where reflection helps a stronger/weaker model on tasks where it didn't help Sonnet.

6. **Flag potential memorization confounds** — note any selected tasks that correspond to very popular PyPI packages (>10k GitHub stars or top-1000 PyPI downloads). These tasks may show inflated baseline performance on some models due to training data exposure. This doesn't exclude them, but should be acknowledged as a limitation and checked in the analysis (e.g., does baseline performance on popular packages differ systematically across models?).

7. **Manual review** — sanity-check the programmatic selection. Remove any tasks with known confounds (e.g., wifiphisher triggers Claude safety refusal — valid result but not useful for cross-model comparison if Qwen doesn't have the same safety filter).

#### Target Size

~40 tasks (target upper end). Rationale: at 3 runs × 2 conditions × 40 tasks = 240 runs per model, this keeps Opus costs under ~$800 (assuming similar per-run cost to Sonnet) while maintaining ≥8 tasks per difficulty tier plus negative controls. The full-batch Sonnet Cohen's d is 0.200 (small effect) — within-tier power will be limited, so maximize n where budget allows. Report a power analysis in the paper for transparency.

#### Output

- `scripts/select-subset.py` (new): reads graded Sonnet batch data, applies the algorithm above, prints selection rationale to stdout, writes output config file.
- `configs/cross-model-subset.json`: task list with per-task metadata:
  ```json
  {
    "tasks": [
      {
        "name": "six",
        "difficulty": "easy",
        "sonnet_baseline_pass_rate": 0.573,
        "sonnet_jarvis_pass_rate": 0.863,
        "sonnet_delta": 0.290,
        "sonnet_confidence": "high",
        "n_baseline": 3,
        "n_jarvis": 3
      }
    ],
    "selection_criteria": {
      "min_confidence": "high",
      "fallback_confidence": "medium",
      "max_medium_confidence_pct": 0.25,
      "ceiling_threshold": 0.99,
      "floor_threshold": 0.02,
      "difficulty_thresholds": [514, 1207],
      "num_negative_controls": 4
    },
    "popularity_flags": ["six", "jinja", "freezegun"],
    "source_batch": "batch_20260318-033128_1f0e",
    "selected_at": "2026-03-27T..."
  }
  ```
- Runner reads this file via `BenchConfig.tasks` to scope experiments.

### 2. Logging & Data Collection

Before running any new experiments, ensure the harness captures everything needed for later analysis regardless of model/condition.

**Must capture per run:**
- Model identity (opus/sonnet/qwen) in RunResult and manifest
- Condition (baseline/jarvis-prompted/baseline-no-plan)
- All journal entries (copy from workspace to results dir after run)
- PLAN.md contents (copy from workspace to results dir after run)
- Full stdout/stderr, wall time, turn count, exit code (already captured)
- Token usage if available from the execution backend
- Test results from Docker grading (already captured)

**Changes needed:**
- `harness/config.py` / `RunResult`: Add `model` field if not already present
- `harness/runner.py`: After run completes, copy `PLAN.md` and `.jarvis/journal/` to results dir
- Manifest should include model as a grouping dimension

### 3. No-Plan Baseline Condition

Add a `BASELINE_NO_PLAN` condition that removes the PLAN.md instruction from the prompt. This isolates planning's contribution. Runs on the curated subset.

**Changes needed:**
- `harness/config.py`: Add `BASELINE_NO_PLAN` to `Condition` enum
- `harness/runner.py`: Add `_build_baseline_no_plan_prompt()` — same as baseline but without the "create a PLAN.md" instruction
- `harness/__main__.py`: Ensure CLI accepts the new condition name

### 4. Opus Runs with Reflection

Run both baseline and JaRVIS-prompted conditions on Opus via Claude Max, on the curated subset.

**Changes needed:**
- `harness/config.py`: Ensure `BenchConfig.model` (or equivalent) is properly threaded through to the Claude CLI invocation. Currently defaults to `claude-sonnet-4-6` — need to make model selectable per-run or per-batch.
- `harness/runner.py`: Pass model to `claude -p` invocation. **Note:** verify the exact mechanism — `claude -p` may use `--model` flag or may require setting model in the settings JSON. Need to test.
- Verify Opus token/timeout implications — Opus is slower and more expensive per token, may need longer timeouts

### 5. OpenHands/Qwen Runner Backend

Adapt the runner to execute tasks via OpenHands on a remote cluster with Qwen. Runs on the curated subset.

**Changes needed:**
- New execution backend in `harness/runner.py` — instead of `claude -p`, invoke OpenHands agent with equivalent prompt
- `harness/config.py`: Add configuration for remote execution (API endpoint, model name, OpenHands settings)
- Same workspace setup, same grading pipeline — only the execution step changes
- Ensure JaRVIS skills work with Qwen (the skills are markdown files read by the agent — should be model-agnostic, but need to verify Qwen follows them)
- Results must be logged in the same format (RunResult, manifest) so the analysis pipeline works unchanged

**Open questions:**
- Which Qwen model exactly? (User to confirm)
- Does OpenHands support JaRVIS skill loading the same way? May need adaptation.
- Remote execution: does the harness run locally and call a remote API, or does it run on the cluster?

---

## Execution Order

1. **Now (parallel)**: Complete Sonnet batch reruns (35 runs) + re-grade; implement logging improvements (model field, journal/plan capture)
2. **After reruns graded**: Run `select-subset.py` to curate task subset, manual review
3. **Then (parallel)**: Implement no-plan baseline condition + make model configurable
4. **After harness ready**: Start Opus runs on subset, start OpenHands adaptation
5. **After OpenHands adapted**: Run Qwen on remote cluster

---

## Verification

For each implementation item:
- `ruff check harness/` and `mypy harness/` pass
- Smoke test with 1-2 tasks to verify the new condition/backend works
- Confirm results appear in manifest with correct model/condition tags
- Confirm journal entries and PLAN.md are captured in results directory
