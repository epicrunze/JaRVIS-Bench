#!/usr/bin/env bash
set -euo pipefail

# Thin wrapper that activates the venv and delegates to python -m harness.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

usage() {
    cat <<'EOF'
Usage: run-eval.sh [OPTIONS]

Run modes:
  --full                        Run all 104 tasks
  --smoke                       Quick smoke test (1 easy task, 1 run)
  --task NAME                   Single task
  --tasks-from FILE             Read task names from file (one per line)

Grade/report only:
  --grade-only BATCH_ID         Grade an existing batch
  --report-only BATCH_ID        Generate report for a batch

Run options (combinable with run modes):
  --condition COND              baseline | jarvis-prompted | both (default: both)
  --runs N                      Runs per task×condition (default: 3)
  --timeout SECS                Per-task timeout (default: 1200)
  --model MODEL                 Claude model override
  -v, --verbose                 Enable debug logging
  -h, --help                    Show this help
EOF
}

# --- Prerequisites ---
if [[ ! -d "$REPO_ROOT/.venv" ]]; then
    echo "Error: .venv/ not found. Run ./scripts/setup.sh first." >&2
    exit 1
fi
if [[ ! -d "$REPO_ROOT/vendor/NL2RepoBench" ]]; then
    echo "Error: vendor/NL2RepoBench/ not found. Run ./scripts/setup.sh first." >&2
    exit 1
fi
if [[ ! -d "$REPO_ROOT/vendor/JaRVIS" ]]; then
    echo "Error: vendor/JaRVIS/ not found. Run ./scripts/setup.sh first." >&2
    exit 1
fi

# shellcheck disable=SC1091
source "$REPO_ROOT/.venv/bin/activate"

# --- Parse args and delegate ---
ARGS=()
VERBOSE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --full)
            ARGS=("run" "--full"); shift ;;
        --smoke)
            ARGS=("run" "--smoke"); shift ;;
        --task)
            ARGS=("run" "--task" "$2"); shift 2 ;;
        --tasks-from)
            ARGS=("run" "--tasks-from" "$2"); shift 2 ;;
        --grade-only)
            ARGS=("grade" "--batch-id" "$2"); shift 2 ;;
        --report-only)
            ARGS=("report" "--batch-id" "$2"); shift 2 ;;
        --condition)
            ARGS+=("--condition" "$2"); shift 2 ;;
        --runs)
            ARGS+=("--runs" "$2"); shift 2 ;;
        --timeout)
            ARGS+=("--timeout" "$2"); shift 2 ;;
        --model)
            ARGS+=("--model" "$2"); shift 2 ;;
        -v|--verbose)
            VERBOSE="-v"; shift ;;
        -h|--help)
            usage; exit 0 ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 1 ;;
    esac
done

if [[ ${#ARGS[@]} -eq 0 ]]; then
    echo "Error: no command specified." >&2
    usage >&2
    exit 1
fi

exec python -m harness ${VERBOSE:+"$VERBOSE"} "${ARGS[@]}"
