#!/usr/bin/env bash

# build-system-prompt.sh — Build a system prompt file for non-gptme harnesses
#
# Loads the gptme.toml [prompt] files via `gptme-util context files` and runs
# context_cmd.
# Output is suitable for --append-system-prompt-file with Claude Code or similar.
#
# Usage:
#   ./scripts/context/build-system-prompt.sh [WORKSPACE]
#   WORKSPACE=/path/to/agent ./scripts/context/build-system-prompt.sh

set -euo pipefail

if [ -n "${1:-}" ]; then
    WORKSPACE="$1"
elif [ -n "${WORKSPACE:-}" ]; then
    : # Use existing WORKSPACE
else
    # Find agent root (directory with gptme.toml) via shared helper
    # shellcheck source=scripts/context/find-agent-root.sh
    . "$(dirname "$0")/find-agent-root.sh"
    WORKSPACE="$(find_agent_root)" || {
        echo "Error: Could not find gptme.toml in any parent directory of $PWD" >&2
        echo "Run from your agent workspace directory, or pass WORKSPACE=/path/to/agent" >&2
        exit 1
    }
fi

# --- Read gptme.toml config ---
read_toml_config() {
    WORKSPACE="$WORKSPACE" python3 -c "
import tomllib, json, sys, os
with open(os.environ['WORKSPACE'] + '/gptme.toml', 'rb') as f:
    cfg = tomllib.load(f)
prompt = cfg.get('prompt', {})
json.dump({
    'context_cmd': prompt.get('context_cmd', ''),
}, sys.stdout)
"
}

CONFIG=$(read_toml_config)
CONTEXT_CMD=$(echo "$CONFIG" | python3 -c "import json,sys; print(json.load(sys.stdin)['context_cmd'])")

# --- Bootstrap identity files ---
# gptme-util resolves [prompt] files exactly as gptme does: glob entries,
# [prompt] exclude and both gptme.toml schemas. gptme is a hard dependency of
# an agent workspace (`pipx install gptme` provides gptme-util).
if ! command -v gptme-util >/dev/null 2>&1; then
    echo "Error: gptme-util not found on PATH (install gptme: pipx install gptme)" >&2
    exit 1
fi

echo "# Bootstrap Identity Files"
echo ""

# Exits 1 with a stderr note when no prompt files are configured; that is an
# empty section, not a build failure.
gptme-util context files --config "$WORKSPACE/gptme.toml" || true

# --- Dynamic context (context_cmd from gptme.toml) ---
echo "# Dynamic Context"
echo ""

if [ -n "$CONTEXT_CMD" ]; then
    # Handle both absolute paths and paths relative to workspace
    case "$CONTEXT_CMD" in
        /*) CMD="$CONTEXT_CMD" ;;
        *)  CMD="$WORKSPACE/$CONTEXT_CMD" ;;
    esac
    # shellcheck disable=SC2086
    $CMD "$WORKSPACE" 2>/dev/null || echo "(context generation failed)"
else
    echo "(no context_cmd configured in gptme.toml)"
fi
