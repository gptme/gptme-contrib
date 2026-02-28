#!/usr/bin/env bash

# build-system-prompt.sh — Build a system prompt file for non-gptme harnesses
#
# Reads gptme.toml to extract bootstrap identity files and runs context_cmd.
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
    # Walk up from CWD to find gptme.toml (agent root, not gptme-contrib root)
    _dir="$PWD"
    while [ "$_dir" != "/" ]; do
        if [ -f "$_dir/gptme.toml" ]; then
            WORKSPACE="$_dir"
            break
        fi
        _dir="$(dirname "$_dir")"
    done
    if [ -z "${WORKSPACE:-}" ]; then
        echo "Error: Could not find gptme.toml in any parent directory of $PWD" >&2
        echo "Run from your agent workspace directory, or pass WORKSPACE=/path/to/agent" >&2
        exit 1
    fi
fi

# --- Read gptme.toml config ---
read_toml_config() {
    WORKSPACE="$WORKSPACE" python3 -c "
import tomllib, json, sys, os
with open(os.environ['WORKSPACE'] + '/gptme.toml', 'rb') as f:
    cfg = tomllib.load(f)
prompt = cfg.get('prompt', {})
json.dump({
    'files': prompt.get('files', []),
    'context_cmd': prompt.get('context_cmd', ''),
}, sys.stdout)
"
}

CONFIG=$(read_toml_config)
CONTEXT_CMD=$(echo "$CONFIG" | python3 -c "import json,sys; print(json.load(sys.stdin)['context_cmd'])")

# --- Bootstrap identity files ---
echo "# Bootstrap Identity Files"
echo ""

python3 -c "import json,sys; [print(f) for f in json.load(sys.stdin)['files']]" <<< "$CONFIG" | while IFS= read -r f; do
    filepath="$WORKSPACE/$f"
    if [ -f "$filepath" ]; then
        echo "## FILE: $f"
        echo ""
        cat "$filepath"
        echo ""
        echo "---"
        echo ""
    fi
done

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
