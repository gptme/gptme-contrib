#!/usr/bin/env bash
# Claude Code PostToolUseSubmit hook — async memory extraction.
#
# Reads the trajectory file from the CC_TRAJECTORY_FILE env var and runs the
# extractor. Configure in .claude/settings.local.json:
#
#   {
#     "hooks": {
#       "PostToolUseSubmit": "gptme-cc-memory-stop-hook"
#     }
#   }
#
# This hook must be on PATH or referenced by absolute path.

set -euo pipefail

# Guard: require CC_TRAJECTORY_FILE — graceful degradation if absent
if [[ -z "${CC_TRAJECTORY_FILE:-}" ]]; then
  exit 0
fi

# Resolve this script's directory (works with symlinks via realpath)
SCRIPT_DIR="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")" && pwd)"
EXTRACTOR="${SCRIPT_DIR}/../extractor.py"

# Fallback: try the pip-installed entry point
if [ ! -f "$EXTRACTOR" ]; then
  if command -v gptme-cc-memory-extract &>/dev/null; then
    exec gptme-cc-memory-extract "$CC_TRAJECTORY_FILE"
  fi
  exit 0  # Silent exit if extractor not found — graceful degradation
fi

exec python3 "$EXTRACTOR" "$CC_TRAJECTORY_FILE"
