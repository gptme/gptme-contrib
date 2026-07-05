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
# Find agent root via shared helper (walks up from $PWD to find gptme.toml)
if [ -n "${1:-}" ]; then
    AGENT_DIR="$1"
else
    # shellcheck source=scripts/context/find-agent-root.sh
    . "$SCRIPT_DIR/find-agent-root.sh"
    AGENT_DIR="$(find_agent_root)"
fi

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
