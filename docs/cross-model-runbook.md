# Cross-Model Experiment Runbook

Task subset selected from Sonnet batch `batch_20260318-033128_1f0e` (40 tasks, stratified by difficulty).
Selection script: `scripts/select-subset.py`. Config: `configs/cross-model-subset.json`.

## Task List

40 tasks in `configs/cross-model-tasks.txt` (one per line, for `--tasks-from`).

| Tier | Count | Tasks |
|------|------:|-------|
| Easy (9) | 9 | docopt-ng, ipytest, pandarallel, parse (from medium? no — check), pytestify, pytz, records, requests-html, xlrd, python-patterns |
| Medium (18) | 18 | coverage_shield, fastapi-users, flask-restful, ftfy, funcy, fuzzywuzzy, mechanicalsoup, parse, pathlib2, pylama, pypinyin, pysondb-v2, python-jose, pythonprojecttemplate, schedule-master, stamina, tinydb, unittest-parametrize |
| Hard (13) | 13 | arguably, boto, databases, dictdatabase, mootdx, pdfplumber-stable, pyjwt, pyquery, python-pytest-cases, rich-click, sortedcontainers, sqlparse, structlog |

6 negative controls (Sonnet ties): requests-html, python-jose, coverage_shield, rich-click, pyjwt, python-pytest-cases.

---

## Opus Runs

Both baseline and JaRVIS conditions, 3 runs each, using Claude Max subscription.

### Run command

```bash
# Runs baseline + jarvis-prompted, both with Opus model
python -m harness run \
  --condition both \
  --model claude-opus-4-6 \
  --tasks-from configs/cross-model-tasks.txt \
  --runs 3 \
  --parallel 3 \
  --idle-timeout 300 \
  --timeout 3600
```

This produces 40 tasks x 2 conditions x 3 runs = **240 runs**.

### Estimated cost

Sonnet averages $2.42/run. Opus is ~3-5x more expensive per token.
Conservative estimate: **$1,500-$3,000** for the full Opus batch.

### After completion

```bash
# Grade the batch
python -m harness grade --batch-id <opus_batch_id> --parallel 4

# Generate reports
python -m harness report --batch-id <opus_batch_id>

# Analyze
python scripts/analyze-results.py <opus_batch_id>
```

### If runs fail mid-batch

```bash
# Resume transient failures (auth errors, usage limits)
python -m harness run --resume-batch <opus_batch_id>

# Resume including timeouts
python -m harness run --resume-batch <opus_batch_id> --include-timeouts
```

---

## Qwen Runs (OpenHands)

Same 40-task subset. Requires OpenHands runner backend (not yet implemented — see neurips-experiment-plan.md Section 5).

```bash
# Placeholder — exact command TBD after OpenHands integration
python -m harness run \
  --condition both \
  --model qwen-TBD \
  --tasks-from configs/cross-model-tasks.txt \
  --runs 3
```

---

## No-Plan Ablation (Sonnet)

Same 40-task subset. Requires `BASELINE_NO_PLAN` condition (not yet implemented — see neurips-experiment-plan.md Section 3).

```bash
# Placeholder — exact command TBD after no-plan condition is added
python -m harness run \
  --condition baseline-no-plan \
  --tasks-from configs/cross-model-tasks.txt \
  --runs 3
```

---

## Files

| File | Purpose |
|------|---------|
| `configs/cross-model-subset.json` | Full subset config with per-task metadata |
| `configs/cross-model-tasks.txt` | Plain task list for `--tasks-from` |
| `scripts/select-subset.py` | Reproducible selection script |
| `docs/neurips-experiment-plan.md` | Full experiment design |
