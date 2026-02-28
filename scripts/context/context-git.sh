#!/bin/bash

# Output git context for gptme agents
# Usage: ./scripts/context/context-git.sh [AGENT_DIR]
#
# Shows git status (file list) and recent commits.
# Uses plain `git status` (not -vv) to avoid prompt blowup from large diffs.
# If AGENT_DIR is not provided, uses parent of the script's directory.

set -e

if [ -n "${1:-}" ]; then
    AGENT_DIR="$1"
else
    AGENT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || (cd "$(dirname "$0")/.." && pwd))"
fi

echo -e "# Git\n"

echo '```git status'
git -C "$AGENT_DIR" status | head -200
echo '```'
echo

echo '```git log --oneline -5'
git -C "$AGENT_DIR" log --oneline -5 2>/dev/null || echo "(no commits)"
echo '```'
