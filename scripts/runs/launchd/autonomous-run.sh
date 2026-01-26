#!/usr/bin/env bash
# Autonomous run script for launchd
#
# This script runs a single autonomous session using the run_loops CLI.
# Customize WORKSPACE to point to your agent workspace.
#
# Usage: ./autonomous-run.sh [--workspace PATH]

set -e

# Load user environment (for PATH, API keys, etc.)
# shellcheck source=/dev/null
[ -f ~/.profile ] && source ~/.profile
# shellcheck source=/dev/null
[ -f ~/.bash_profile ] && source ~/.bash_profile
# shellcheck source=/dev/null
[ -f ~/.zshrc ] && source ~/.zshrc 2>/dev/null

# Configuration - UPDATE THIS for your setup
WORKSPACE="${WORKSPACE:-$HOME/gptme-agent}"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --workspace)
            WORKSPACE="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Validate workspace
if [ ! -d "$WORKSPACE" ]; then
    echo "Error: Workspace not found: $WORKSPACE"
    echo "Set WORKSPACE environment variable or use --workspace PATH"
    exit 1
fi

cd "$WORKSPACE"

# Run autonomous session
# Option 1: If run_loops is installed via uv (recommended)
if command -v uv &>/dev/null; then
    exec uv run python3 -m run_loops.cli autonomous --workspace "$WORKSPACE"
# Option 2: If run_loops is installed in system Python
elif python3 -c "import run_loops" &>/dev/null; then
    exec python3 -m run_loops.cli autonomous --workspace "$WORKSPACE"
else
    echo "Error: run_loops package not found"
    echo "Install with: uv add gptme-runloops"
    exit 1
fi
