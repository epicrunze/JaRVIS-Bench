#!/usr/bin/env bash
set -euo pipefail

# Select subsets of NL2Repo-Bench tasks by difficulty.
# Outputs task names to stdout (one per line), composable with run-eval.sh.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEST_FILES_DIR="$REPO_ROOT/vendor/NL2RepoBench/test_files"

usage() {
    cat <<'EOF'
Usage: select-tasks.sh [OPTIONS]

Options:
  --easy          Tasks with ≤50 test cases
  --medium        Tasks with 51-299 test cases
  --hard          Tasks with ≥300 test cases
  --all           All tasks
  --sample N      Random sample of N tasks (combinable with difficulty)
  --category NAME Not supported (NL2RepoBench has no category metadata)
  -h, --help      Show this help
EOF
}

if [[ ! -d "$TEST_FILES_DIR" ]]; then
    echo "Error: NL2RepoBench not found at $TEST_FILES_DIR" >&2
    echo "Run ./scripts/setup.sh first." >&2
    exit 1
fi

DIFFICULTY=""
SAMPLE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --easy)    DIFFICULTY="easy"; shift ;;
        --medium)  DIFFICULTY="medium"; shift ;;
        --hard)    DIFFICULTY="hard"; shift ;;
        --all)     DIFFICULTY="all"; shift ;;
        --sample)
            SAMPLE="$2"; shift 2 ;;
        --category)
            echo "Not supported: NL2RepoBench has no category metadata" >&2
            exit 1 ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 1 ;;
    esac
done

if [[ -z "$DIFFICULTY" ]]; then
    echo "Error: specify --easy, --medium, --hard, or --all" >&2
    usage >&2
    exit 1
fi

# Collect tasks matching difficulty filter
tasks=()
for count_file in "$TEST_FILES_DIR"/*/test_case_count.txt; do
    task_dir="$(dirname "$count_file")"
    task_name="$(basename "$task_dir")"
    count="$(cat "$count_file" 2>/dev/null | tr -d '[:space:]')"
    count="${count:-0}"

    case "$DIFFICULTY" in
        easy)   [[ "$count" -le 50 ]]  || continue ;;
        medium) [[ "$count" -gt 50 && "$count" -lt 300 ]] || continue ;;
        hard)   [[ "$count" -ge 300 ]] || continue ;;
        all)    ;;  # no filter
    esac
    tasks+=("$task_name")
done

# Sort for deterministic output
IFS=$'\n' sorted=($(sort <<<"${tasks[*]}")); unset IFS

if [[ -n "$SAMPLE" ]]; then
    printf '%s\n' "${sorted[@]}" | shuf -n "$SAMPLE"
else
    printf '%s\n' "${sorted[@]}"
fi
