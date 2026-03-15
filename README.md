# JaRVIS-Bench

A/B evaluation framework measuring whether [JaRVIS](https://github.com/epicrunze/JaRVIS) reflective journaling improves Claude Code's performance on long-horizon repository generation tasks.

Uses [NL2Repo-Bench](https://github.com/multimodal-art-projection/NL2RepoBench) (104 Python library generation tasks) as the task and evaluation infrastructure.

## Quick Start

```bash
./scripts/setup.sh    # Install deps (uv), clone NL2RepoBench + JaRVIS
source .venv/bin/activate
./scripts/run-eval.sh --smoke  # Run a quick smoke test
```

## Build Plan

See [scaffold.md](scaffold.md) for the full phased build plan.
