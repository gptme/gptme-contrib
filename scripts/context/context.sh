#!/bin/bash

# Build context for gptme agents
# Usage: ./scripts/context/context.sh [AGENT_DIR]
#
# Orchestrates context generation by calling component scripts:
#   - context-journal.sh  — Recent journal entries
#   - context-workspace.sh — Workspace tree structure
#   - context-git.sh — Git status and recent commits
#   - gptodo status — Task status (if gptodo is installed)
#
# If AGENT_DIR is not provided, uses parent of the script's directory.
# Agents can override this script or call individual components directly.

set -e

export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Default AGENT_DIR: use git toplevel if available, else resolve from $0 (symlink-safe)
if [ -n "${1:-}" ]; then
    AGENT_DIR="$1"
else
    AGENT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || (cd "$(dirname "$0")/.." && pwd))"
fi

# Ensure component scripts are executable
chmod +x "$SCRIPT_DIR"/context-*.sh

echo "# Context Summary"
echo
echo "Generated on: $(date)"
echo
echo "---"
echo

# Journal
"$SCRIPT_DIR"/context-journal.sh "$AGENT_DIR"
echo

# Tasks
echo -e "# Tasks\n"
if command -v gptodo &> /dev/null; then
    echo -e "Output of \`gptodo status --compact\` command:\n"
    (cd "$AGENT_DIR" && gptodo status --compact)
else
    echo -e "(Task management CLI not installed - install gptodo from gptme-contrib)\n"
    echo -e "See: uv tool install git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptodo\n"
fi
echo

# Workspace
"$SCRIPT_DIR"/context-workspace.sh "$AGENT_DIR"
echo

# Git
"$SCRIPT_DIR"/context-git.sh "$AGENT_DIR"
