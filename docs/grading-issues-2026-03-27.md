# Grading Issues Investigation — 2026-03-27

## Summary

Post-regrade analysis of `batch_20260318-033128_1f0e` (624 runs, 104 tasks) uncovered two categories of grading bugs that inflate apparent baseline wins and suppress JaRVIS scores.

### Current headline numbers (after partial fixes applied)

| Metric | Value |
|--------|-------|
| Mean pass rate delta | +4.3% (JaRVIS over baseline) |
| Median pass rate delta | +6.5% |
| Cohen's d | 0.131 |
| Win/Tie/Loss | 48/26/28 |
| Tasks compared | 102 paired |

---

## Bug 1: `shutil.copytree` fails on `.venv` broken symlinks (FIXED)

**Root cause:** JaRVIS runs that create virtual environments produce `.venv/bin/python` symlinks pointing to container-internal paths. When the grader copies the workspace via `shutil.copytree()`, it fails with `FileNotFoundError` on these broken symlinks. `test_result` stays `null`.

**Fix applied:** Added `ignore=shutil.ignore_patterns(...)` to `_stage_workspace()` in `harness/grader.py`, skipping `.venv`, `__pycache__`, `.pytest_cache`, `.git`, `.jarvis`, `.claude`, `node_modules`.

**Affected runs (regraded successfully):**

| Run | Before fix | After fix |
|-----|-----------|-----------|
| `math-verify_jarvis-prompted_..._d957` | null (0/0) | **154/192 = 80.2%** |
| `math-verify_jarvis-prompted_..._96d6` | null (0/0) | **164/192 = 85.4%** |

**Impact:** math-verify flipped from -53.3% (biggest baseline win) to +1.9% (slight JaRVIS win). Headline mean delta improved from +3.8% to +4.3%.

## Bug 2: Mixed-case Docker image tags (FIXED)

**Root cause:** `_build_test_image()` used raw `run_id` in the Docker tag. Task names with mixed case (e.g., `more-Itertools`) produce invalid tags. Docker requires lowercase.

**Fix applied:** Added `.lower()` to image tag construction in `_build_test_image()`, `_run_tests_in_container()`, and `_cleanup_docker()` in `harness/grader.py`.

**Affected runs:**

| Run | Before fix | After fix |
|-----|-----------|-----------|
| `more-Itertools_jarvis-prompted_..._0fd2` | null (grader crash) | 0/682 = 0.0% (pytest timed out at 7%) |

**Impact:** Minimal — more-Itertools scores 0% across all conditions anyway. But the fix prevents future mixed-case task names from failing silently.

---

## Bug 3: Pytest per-command timeout too short (NOT YET FIXED)

**Root cause:** `_run_tests_in_container()` uses a 600s timeout per `docker exec` command (line 167 of `harness/grader.py`). Some test runs hang on individual tests (socket/network/GPU tests that block indefinitely), causing the entire pytest run to time out even though the test suite would otherwise complete in seconds.

**This is NOT a "test suite too slow" problem.** In every affected task, sibling runs of the same code complete in 1-226s. The timeouts are caused by individual tests that hang intermittently.

### Full inventory of pytest-timeout-affected runs

#### pytorch-grad-cam (4 runs timed out, 2 completed fine)

| Run | Condition | Grade | Test time | Status |
|-----|-----------|-------|-----------|--------|
| b367 | baseline | 0/178=0% | — | **PYTEST_TIMEOUT@1%** |
| caa9 | baseline | 0/178=0% | — | **PYTEST_TIMEOUT@1%** |
| ef1a | baseline | 178/178=100% | 226s | OK |
| 08d6 | jarvis | 0/178=0% | — | **PYTEST_TIMEOUT@1%** |
| 2833 | jarvis | 178/178=100% | 175s | OK |
| b7b3 | jarvis | 0/178=0% | — | **PYTEST_TIMEOUT@1%** |

**Diagnosis:** Successful runs take 175-226s. 4 runs hang at 1% — a single early test blocks indefinitely (likely GPU/CUDA-related). Both conditions affected equally (2B, 2J). Current scores: 33.3% / 33.3% (tie). **Regrade priority: HIGH** — would likely become 100%/100% or close, raising both conditions equally.

#### databases (2 runs timed out at 100% progress)

| Run | Condition | Grade | Test time | Status |
|-----|-----------|-------|-----------|--------|
| 58ac | baseline | 0/0 | — | AGENT_TIMEOUT |
| cf4b | baseline | 23/150=15% | 3s | OK |
| c22d | baseline | 5/150=3% | 3s | OK |
| e88c | jarvis | 62/150=41% | 482s | **PYTEST_TIMEOUT@100%** |
| 30ae | jarvis | 0/0 | — | AGENT_TIMEOUT |
| 44af | jarvis | 64/150=43% | 523s | **PYTEST_TIMEOUT@100%** |

**Diagnosis:** The 2 JaRVIS runs completed 100% of tests but were killed at 600s before output parsing finished. They took 482-523s. Baseline runs that completed took only 3s — completely different code produced different test execution time. The JaRVIS code triggers slow test paths (likely actual database connections vs mocks). **Regrade priority: HIGH** — scores may be higher than reported 41-43% since some late tests may have been lost. Bumping to 1200s should suffice.

#### dbutils (3 runs timed out at exactly 55%)

| Run | Condition | Grade | Test time | Status |
|-----|-----------|-------|-----------|--------|
| bc2a | baseline | 0/140=0% | — | **PYTEST_TIMEOUT@55%** |
| 35c0 | baseline | 0/0 | — | AGENT_TIMEOUT |
| 1d57 | baseline | 0/0 | — | AGENT_TIMEOUT |
| 7cab | jarvis | 0/140=0% | — | **PYTEST_TIMEOUT@55%** |
| dd0d | jarvis | 0/140=0% | — | **PYTEST_TIMEOUT@55%** |
| 3661 | jarvis | 34/140=24% | 1s | OK |

**Diagnosis:** 3 runs from both conditions hang at exactly 55% — a specific test at that position blocks indefinitely. The successful run took 1s. Both conditions affected. Current: baseline=0.0%, jarvis=8.1%. **Regrade priority: HIGH** — per-test timeout would let tests after the hang complete. Would affect both conditions.

#### boltons (2 runs timed out, 1 completed in 11s)

| Run | Condition | Grade | Test time | Status |
|-----|-----------|-------|-----------|--------|
| 5e22 | baseline | 0/423=0% | — | OK (0% but no timeout) |
| bc52 | baseline | 0/423=0% | — | OK |
| 2c23 | baseline | 0/423=0% | — | **PYTEST_TIMEOUT@36%** |
| 34f9 | jarvis | 0/423=0% | — | OK |
| 95bd | jarvis | 124/423=29% | 11s | OK |
| 26de | jarvis | 0/423=0% | — | **PYTEST_TIMEOUT@52%** |

**Diagnosis:** Successful run took 11s. Two runs hang at 36% and 52% — different hang points. The 0% non-timeout runs may have import errors (different issue). Current: baseline=0.0%, jarvis=9.8%. **Regrade priority: MEDIUM** — per-test timeout would help the 2 hanging runs, but 4 other runs score 0% for non-timeout reasons.

#### tenacity (1 run timed out, successful runs take 2-3s)

| Run | Condition | Grade | Test time | Status |
|-----|-----------|-------|-----------|--------|
| 81e0 | baseline | 97/124=78% | 3s | OK |
| 97ae | baseline | 0/0 | — | AGENT_TIMEOUT |
| 010c | baseline | 0/124=0% | — | **PYTEST_TIMEOUT@18%** |
| cdd6 | jarvis | 107/124=86% | 2s | OK |
| 551d | jarvis | 102/124=82% | 2s | OK |
| 2629 | jarvis | 105/124=85% | 2s | OK |

**Diagnosis:** Successful runs take 2-3s. One baseline run hangs at 18%. Current: baseline=39.1% (dragged down by 0%), jarvis=84.4%. **Regrade priority: HIGH** — this is currently reported as a +45.3% JaRVIS win. If the baseline 0% run becomes ~78%, baseline avg jumps to ~52%, narrowing delta to ~32%. Significant impact on the headline numbers.

#### dictdatabase (1 run timed out, successful runs take 3-203s)

| Run | Condition | Grade | Test time | Status |
|-----|-----------|-------|-----------|--------|
| aa71 | baseline | 0/594=0% | — | **PYTEST_TIMEOUT@54%** |
| a7c2 | baseline | 228/594=38% | 203s | OK |
| fb70 | baseline | 250/594=42% | 43s | OK |
| c33a | jarvis | 312/594=53% | 5s | OK |
| d203 | jarvis | 274/594=46% | 4s | OK |
| 43ad | jarvis | 260/594=44% | 3s | OK |

**Diagnosis:** JaRVIS runs take 3-5s. Baseline runs take 43-203s — one timed out at 54%. The 203s outlier suggests baseline code triggers slow test paths. Current: baseline=26.8%, jarvis=47.5%. **Regrade priority: MEDIUM** — baseline's 0% would likely become ~38-42%, narrowing the delta from +20.7% to ~+7%.

#### tsfresh (1 run timed out, successful runs take 7-40s)

| Run | Condition | Grade | Test time | Status |
|-----|-----------|-------|-----------|--------|
| 940c | baseline | 0/0 | — | AGENT_TIMEOUT |
| b347 | baseline | 64/371=17% | 18s | OK |
| 36f0 | baseline | 73/371=20% | 40s | OK |
| 1b14 | jarvis | 74/371=20% | 21s | OK |
| cbcc | jarvis | 0/371=0% | — | **PYTEST_TIMEOUT@28%** |
| 9363 | jarvis | 20/371=5% | 7s | OK |

**Diagnosis:** Successful runs take 7-40s. One JaRVIS run hangs at 28%. Current: baseline=18.5%, jarvis=8.4% (-10.0% baseline win). **Regrade priority: MEDIUM** — if the 0% JaRVIS run becomes ~15-20%, JaRVIS avg rises to ~13-15%, narrowing the baseline win.

#### wsgidav (1 run timed out, successful runs take 30-120s)

| Run | Condition | Grade | Test time | Status |
|-----|-----------|-------|-----------|--------|
| bf68 | baseline | 5/36=14% | 120s | OK |
| 0dfa | baseline | 0/36=0% | — | **PYTEST_TIMEOUT@68%** |
| 783e | baseline | 10/36=28% | 30s | OK |
| 0166 | jarvis | 11/36=31% | 90s | OK |
| 8a4d | jarvis | 6/36=17% | 31s | OK |
| f864 | jarvis | 0/0 | — | AGENT_TIMEOUT |

**Diagnosis:** Successful runs take 30-120s. One baseline run hangs at 68%. Current: baseline=13.9%, jarvis=23.6%. **Regrade priority: LOW** — small test suite (36 tests), moderate impact.

#### cachier (2 runs hung at 0%)

| Run | Condition | Grade | Test time | Status |
|-----|-----------|-------|-----------|--------|
| f885 | baseline | 0/0 | — | AGENT_TIMEOUT |
| 763d | baseline | 0/180=0% | — | **PYTEST_TIMEOUT@0%** |
| ddfe | baseline | 0/0 | — | AGENT_TIMEOUT |
| b99b | jarvis | 0/0 | — | AGENT_TIMEOUT |
| ef33 | jarvis | 0/180=0% | — | **PYTEST_TIMEOUT@0%** |
| fac0 | jarvis | 0/0 | — | AGENT_TIMEOUT |

**Diagnosis:** Both runs hang at 0% — likely an import-time hang (database connection, network call during module import). No successful runs exist for comparison. Current: 0.0% / 0.0%. **Regrade priority: SKIP** — per-test timeout won't help import hangs. Both conditions affected equally.

#### freezegun (2 runs hung at 0%)

| Run | Condition | Grade | Test time | Status |
|-----|-----------|-------|-----------|--------|
| 51b6 | baseline | 0/133=0% | — | **PYTEST_TIMEOUT@0%** |
| 4435 | baseline | 0/0 | — | AGENT_TIMEOUT |
| 8ba8 | baseline | 0/133=0% | — | OK (0% no timeout) |
| 134d | jarvis | 0/0 | — | AGENT_TIMEOUT |
| cd8b | jarvis | 0/133=0% | — | OK |
| a776 | jarvis | 0/133=0% | — | **PYTEST_TIMEOUT@0%** |

**Diagnosis:** Hung at 0%. Other non-timeout runs also score 0% — the code is just wrong. **Regrade priority: SKIP**.

#### more-Itertools (1 run, 7% progress on 683 tests)

All baseline runs are AGENT_TIMEOUT (no code). Only 1 JaRVIS run has code. It got through 7% of 683 tests before the 600s timeout. Estimated full suite time: ~2+ hours. **Regrade priority: SKIP** — not practical.

#### ydata-profiling (1 run at 66% progress on 2182 tests)

All runs score 0% across both conditions. One baseline run got to 66% before timeout. 2182 tests is a massive suite. **Regrade priority: SKIP** — both conditions at 0%, no impact on comparison.

---

## Recommended fix: per-test timeout via pytest-timeout

Instead of bumping the global 600s timeout, install `pytest-timeout` in the Docker test image and add `--timeout=120` to pytest commands. This kills individual hanging tests after 120s while allowing the rest of the suite to complete.

**Implementation approach:**
1. In `_run_tests_in_container()`, prepend `pip install pytest-timeout &&` to the first pytest command, or modify the Dockerfile to pre-install it
2. Inject `--timeout=120` into any pytest command in `test_commands`
3. Bump the per-command timeout from 600s to 1800s (safety margin)
4. For `databases` specifically, the 600s global timeout just needs to be higher (tests take 500s legitimately)

## Runs to regrade after fix

### Priority 1 (high impact on results)

| Run ID | Task | Condition | Current | Expected impact |
|--------|------|-----------|---------|-----------------|
| `databases_jarvis-prompted_..._e88c` | databases | jarvis | 41.3% | Higher (tests finished, output truncated) |
| `databases_jarvis-prompted_..._44af` | databases | jarvis | 42.7% | Higher (same) |
| `tenacity_baseline_..._010c` | tenacity | baseline | 0% | ~78% (narrows +45.3% JaRVIS win to ~+32%) |
| `pytorch-grad-cam_baseline_..._b367` | pytorch-grad-cam | baseline | 0% | ~100% (single test hang) |
| `pytorch-grad-cam_baseline_..._caa9` | pytorch-grad-cam | baseline | 0% | ~100% |
| `pytorch-grad-cam_jarvis-prompted_..._08d6` | pytorch-grad-cam | jarvis | 0% | ~100% |
| `pytorch-grad-cam_jarvis-prompted_..._b7b3` | pytorch-grad-cam | jarvis | 0% | ~100% |
| `dbutils_baseline_..._bc2a` | dbutils | baseline | 0% | ~24% (per-test timeout) |
| `dbutils_jarvis-prompted_..._7cab` | dbutils | jarvis | 0% | ~24% |
| `dbutils_jarvis-prompted_..._dd0d` | dbutils | jarvis | 0% | ~24% |

### Priority 2 (moderate impact)

| Run ID | Task | Condition | Current | Expected impact |
|--------|------|-----------|---------|-----------------|
| `dictdatabase_baseline_..._aa71` | dictdatabase | baseline | 0% | ~38-42% (narrows +20.7% delta) |
| `tsfresh_jarvis-prompted_..._cbcc` | tsfresh | jarvis | 0% | ~15-20% (narrows -10% baseline win) |
| `boltons_baseline_..._2c23` | boltons | baseline | 0% | Unknown |
| `boltons_jarvis-prompted_..._26de` | boltons | jarvis | 0% | Unknown |
| `wsgidav_baseline_..._0dfa` | wsgidav | baseline | 0% | ~14-28% |

### Skip (no impact or impractical)

- cachier (hung at 0%, import hang)
- freezegun (hung at 0%, code is wrong anyway)
- more-Itertools (2+ hours for 683 tests)
- ydata-profiling (both conditions 0%, 2182 tests)

## Expected impact on headline numbers

Fixing the pytest timeout issue will primarily:
1. **Raise baseline scores** for tenacity, dictdatabase, pytorch-grad-cam — narrowing some inflated JaRVIS wins
2. **Raise both conditions equally** for pytorch-grad-cam, dbutils — no net effect on delta
3. **Raise JaRVIS scores** for databases, tsfresh — widening some JaRVIS wins

Net effect is hard to predict without regrading, but the comparison becomes more accurate either way.
