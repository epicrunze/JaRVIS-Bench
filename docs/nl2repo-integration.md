# NL2Repo-Bench Integration

How JaRVIS-Bench interfaces with [NL2Repo-Bench](https://github.com/multimodal-art-projection/NL2RepoBench) for task definitions and evaluation.

## Task Structure

Each task lives in `vendor/NL2RepoBench/test_files/{project_name}/` with:

| File | Purpose |
|------|---------|
| `start.md` | Natural-language specification (the prompt given to the coding agent) |
| `test_case_count.txt` | Expected number of test cases |
| `test_commands.json` | Shell commands to execute for testing (typically pytest invocations) |
| `test_files.json` | List of test file references used by the evaluation harness |
| `image.tar` (optional) | Custom Docker image for tasks with special dependencies |

## Evaluation Flow

Based on NL2RepoBench's `post_processor.py`:

1. **Package workspace**: The completed workspace is packaged into a Docker image. A Dockerfile is generated that uses the base image and COPYs the workspace contents into the container.

2. **Run test commands**: Each command from `test_commands.json` is executed inside the container. These are typically pytest invocations against the project's upstream test suite.

3. **Parse results**: Pytest output is parsed with regex patterns:
   - `(\d+) passed` — number of passing tests
   - `(\d+) failed` — number of failing tests
   - `(\d+) error` — number of erroring tests

4. **Compute pass rate**: `pass_rate = passed / total` where `total = passed + failed + errors`.

## What We Reuse

**The post-processing/evaluation logic**: Build a Docker image from a completed workspace, run test commands inside it, parse pytest output, compute pass rate. This will be wrapped in `harness/grader.py` (Phase 3).

## What We Replace

**The coding agent**: NL2RepoBench uses OpenHands as the coding agent. We replace it with Claude Code (both vanilla and with JaRVIS skills). The orchestration layer in `harness/runner.py` handles invoking Claude Code instead of OpenHands.

We do NOT need:
- OpenHands orchestration layer (`main.py`)
- The agent configuration / sandbox setup
- The test data service (`test_data_service.py`) — we run evaluation directly

## Docker Setup

- **Base image**: `docker.all-hands.dev/all-hands-ai/openhands:0.56`
- **Per-task images**: Some tasks include an `image.tar` with pre-installed dependencies. These should be loaded with `docker load` before evaluation.
- **Evaluation container**: Built dynamically per task — base image + COPY workspace + run test commands.

## Key Files in NL2RepoBench

| File | What it does |
|------|-------------|
| `main.py` | Main execution entry point (uses OpenHands — we skip this) |
| `only_test.py` | Standalone test runner — runs evaluation on completed workspaces |
| `post_processor.py` | Core evaluation logic: Docker build, test execution, result parsing |
| `test_data_service.py` | Serves test data to evaluation containers |
| `config.json` | Task configuration and metadata |
| `test_files/` | All 104 task directories |

## Integration Plan

In `harness/grader.py` we will:

1. Accept a completed workspace path and task name
2. Read `test_commands.json` and `test_case_count.txt` from the task directory
3. Build a Docker image: base image + workspace contents
4. Run each test command inside the container
5. Parse pytest output for pass/fail/error counts
6. Return a structured result with pass rate and raw output

This keeps NL2RepoBench unmodified while extracting only the evaluation logic we need.
