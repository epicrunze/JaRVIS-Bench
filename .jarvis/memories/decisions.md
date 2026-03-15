# Key Decisions

## Consolidated
No consolidated decisions yet.

## Recent
- pyproject.toml needs explicit `[tool.setuptools.packages.find] include = ["harness*"]` because the repo has multiple top-level directories (vendor, results, workspaces) that confuse setuptools auto-discovery.
