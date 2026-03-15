#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR_DIR="$REPO_ROOT/vendor"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✔${NC} $1"; }
fail() { echo -e "  ${RED}✘${NC} $1"; }
warn() { echo -e "  ${YELLOW}!${NC} $1"; }

READY=0
MISSING=0

inc_ready()   { READY=$((READY + 1)); }
inc_missing() { MISSING=$((MISSING + 1)); }

echo "=== JaRVIS-Bench Setup ==="
echo

# --- Prerequisite checks ---
echo "Checking prerequisites..."

# claude CLI
if command -v claude &>/dev/null; then
    ok "claude CLI found: $(command -v claude)"
    inc_ready
else
    fail "claude CLI not found — install from https://docs.anthropic.com/en/docs/claude-code"
    inc_missing
fi

# docker
if command -v docker &>/dev/null; then
    if docker info &>/dev/null 2>&1; then
        ok "docker available and running"
        inc_ready
    else
        fail "docker found but daemon not running — start Docker"
        inc_missing
    fi
else
    fail "docker not found — install from https://docs.docker.com/get-docker/"
    inc_missing
fi

# Python 3.11+
if command -v python3 &>/dev/null; then
    PY_VERSION=$(python3 --version 2>&1 | grep -oP '\d+\.\d+')
    PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 11 ]; then
        ok "Python $PY_VERSION"
        inc_ready
    else
        fail "Python $PY_VERSION found — need 3.11+"
        inc_missing
    fi
else
    fail "python3 not found"
    inc_missing
fi

# git
if command -v git &>/dev/null; then
    ok "git found"
    inc_ready
else
    fail "git not found"
    inc_missing
fi

echo

# --- Vendor dependencies ---
echo "Setting up vendor dependencies..."
mkdir -p "$VENDOR_DIR"

# NL2RepoBench
if [ -d "$VENDOR_DIR/NL2RepoBench" ]; then
    ok "NL2RepoBench already cloned"
else
    echo "  Cloning NL2RepoBench..."
    if git clone https://github.com/multimodal-art-projection/NL2RepoBench.git "$VENDOR_DIR/NL2RepoBench"; then
        ok "NL2RepoBench cloned"
    else
        fail "Failed to clone NL2RepoBench"
        inc_missing
    fi
fi

# JaRVIS
if [ -d "$VENDOR_DIR/JaRVIS" ]; then
    ok "JaRVIS already cloned"
else
    echo "  Cloning JaRVIS..."
    if git clone https://github.com/epicrunze/JaRVIS.git "$VENDOR_DIR/JaRVIS"; then
        ok "JaRVIS cloned"
    else
        fail "Failed to clone JaRVIS"
        inc_missing
    fi
fi

echo

# --- Docker images ---
echo "Pulling Docker images..."
if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
    if docker pull docker.all-hands.dev/all-hands-ai/openhands:0.56 2>/dev/null; then
        ok "openhands:0.56 image pulled"
    else
        warn "Could not pull openhands:0.56 — you may not need it immediately"
    fi
else
    warn "Docker not available — skipping image pull"
fi

echo

# --- Install Python deps ---
echo "Installing Python package..."
if command -v uv &>/dev/null; then
    if [ ! -d "$REPO_ROOT/.venv" ]; then
        uv venv "$REPO_ROOT/.venv"
    fi
    if uv pip install -e "$REPO_ROOT" 2>/dev/null; then
        ok "jarvis-bench installed (uv)"
    else
        fail "uv pip install -e . failed"
        inc_missing
    fi
else
    fail "uv not found — install from https://docs.astral.sh/uv/"
    inc_missing
fi

echo

# --- Summary ---
echo "=== Summary ==="
echo -e "  ${GREEN}Ready:${NC}   $READY"
if [ "$MISSING" -gt 0 ]; then
    echo -e "  ${RED}Missing:${NC} $MISSING"
fi

# List NL2RepoBench tasks
if [ -d "$VENDOR_DIR/NL2RepoBench/test_files" ]; then
    TASK_COUNT=$(ls -d "$VENDOR_DIR/NL2RepoBench/test_files"/*/ 2>/dev/null | wc -l)
    echo
    echo "  Found $TASK_COUNT NL2RepoBench tasks in vendor/NL2RepoBench/test_files/"
fi

# List JaRVIS skills
if [ -d "$VENDOR_DIR/JaRVIS/skills" ]; then
    SKILL_COUNT=$(ls -d "$VENDOR_DIR/JaRVIS/skills"/*/ 2>/dev/null | wc -l)
    echo "  Found $SKILL_COUNT JaRVIS skills in vendor/JaRVIS/skills/"
fi

echo
if [ "$MISSING" -eq 0 ]; then
    echo -e "${GREEN}All prerequisites met. Ready to run evaluations.${NC}"
else
    echo -e "${YELLOW}Some prerequisites missing. Fix the items above before running evaluations.${NC}"
fi
