---
name: analyze-batch
description: Use when reviewing a completed JaRVIS-Bench batch to understand why tests passed or failed, identify failure patterns, and get suggestions for prompt and journaling improvements. Requires batch_id.
---

# Analyze Batch Skill

Orchestrates a 2-level hierarchical analysis of a graded JaRVIS-Bench batch. Group-lead agents dispatch their own tier-1 analysts, reducing orchestration burden from O(runs) to O(groups).

## Usage

```
/analyze-batch <batch_id>
```

## Architecture

```
Skill operator (this session)
  ├── Step 1: CLI prepares contexts + computes partitions
  ├── Step 2: Dispatch N group-lead agents in parallel
  │     └── Each group-lead:
  │           ├── Reads context files for its assigned runs
  │           ├── Dispatches tier-1 analyst agents in parallel
  │           ├── Collects tier-1 results (fresh in context, not from disk)
  │           ├── Writes tier-1 reports to disk
  │           └── Writes group summary to disk
  └── Step 3: Read all group summaries, synthesize final report
```

## Steps

### Step 1: Validate & Prepare

1. Parse `batch_id` from the skill args. If missing, ask the user.
2. Run the CLI to prepare all analysis contexts:
   ```bash
   uv run python -m harness analyze --batch-id <batch_id>
   ```
3. Read `analysis/<batch_id>/metadata.json` to get batch info (`total_runs`, `group_count`, `partitions`, `conditions`, `tasks`).

### Step 2: Dispatch Group Leads (Parallel)

For each partition in `metadata.partitions`:

1. Build the group-lead prompt using `format_group_lead_prompt`:
   ```python
   from harness.analyzer import format_group_lead_prompt
   from harness.config import BenchConfig
   config = BenchConfig(project_root=Path("."))
   prompt = format_group_lead_prompt(batch_id, group_index, run_ids, config)
   ```
   Or read `metadata.json` and construct the prompt manually using the partition's run IDs and context file paths.

2. Dispatch an Agent for each group with:
   - **subagent_type**: `general-purpose`
   - **description**: `"Group {index} analysis ({n} runs)"`
   - **prompt**: The group-lead prompt (from `format_group_lead_prompt` or constructed manually)
   - **run_in_background**: `true`

3. Dispatch ALL group-lead agents in a **SINGLE message** for maximum parallelism.

4. Wait for all group leads to complete.

5. **Verify outputs**: Count files in `analysis/<batch_id>/runs/` and `analysis/<batch_id>/summaries/` directories. Expected:
   - `runs/` should have one `.md` per run (total_runs files)
   - `summaries/` should have one `summary_{n}.md` per group (group_count files)

6. **Update metadata**: Read `metadata.json`, set `finished_at` to current UTC timestamp, and write it back.

### Step 3: Final Synthesis

1. Read all group summaries from `analysis/<batch_id>/summaries/`.
2. Synthesize a final consolidated report covering:
   - **Executive Summary**: Key findings in 3-5 bullet points.
   - **Common Failure Patterns**: Ranked by frequency across all runs. Include specific code-level root causes.
   - **Baseline vs JaRVIS Comparison**: Which condition performed better and why. Task-level breakdown where interesting.
   - **Top Prompt Improvement Recommendations**: Specific, actionable changes to make the agent more effective.
   - **JaRVIS Journaling Improvement Recommendations**: What the reflection system should capture better.
   - **Task Difficulty Analysis**: Which tasks are easy/hard and why.
3. Save the report to `analysis/<batch_id>/report.md`.
4. Present key findings to the user in a concise summary.

## Important Notes

- Use the Agent tool (subagent_type: `general-purpose`) for ALL subagent dispatch. Never use `claude -p` subprocess.
- Dispatch all group-lead agents in a SINGLE message for maximum parallelism.
- Group leads handle their own tier-1 dispatch — you do NOT dispatch tier-1 agents directly.
- The CLI command (`uv run python -m harness analyze`) only prepares data. This skill handles all agent orchestration.
- All persistence goes to `analysis/<batch_id>/` — this directory is gitignored.
