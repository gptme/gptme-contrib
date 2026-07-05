#!/bin/bash
# find-agent-root.sh — Find the agent workspace root (directory containing gptme.toml)
#
# The agent root is the nearest ancestor directory (including START_DIR) that
# contains a gptme.toml file. Falls back to git toplevel if no gptme.toml found.
#
# Usage (as command):
#   ./find-agent-root.sh [START_DIR]    → prints path, exits 0 or 1
#
# Usage (sourced):
#   . /path/to/find-agent-root.sh
#   AGENT_DIR="$(find_agent_root)"            # start from $PWD
#   AGENT_DIR="$(find_agent_root /some/dir)"  # start from explicit dir

find_agent_root() {
    local dir="${1:-$PWD}"
    local fallback=""
    while [ "$dir" != "/" ]; do
        if [ -f "$dir/gptme.toml" ]; then
            # Prefer gptme.toml with [agent] section — marks an actual agent workspace.
            # A bare gptme.toml (e.g. in gptme-contrib itself if it ever gets one)
            # should not be mistaken for an agent root.
            if grep -q '^\[agent\]' "$dir/gptme.toml"; then
                echo "$dir"
                return 0
            fi
            # Remember first gptme.toml without [agent] as fallback
            [ -z "$fallback" ] && fallback="$dir"
        fi
        dir="$(dirname "$dir")"
    done
    # Use fallback (gptme.toml without [agent]) if no agent-specific one found
    if [ -n "$fallback" ]; then
        echo "$fallback"
        return 0
    fi
    # Last resort: git toplevel (works even without gptme.toml)
    git -C "${1:-$PWD}" rev-parse --show-toplevel 2>/dev/null && return 0
    return 1
}

# If executed directly (not sourced), run and output the result
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    result="$(find_agent_root "${1:-}")" || {
        echo "Error: Could not find agent root from ${1:-$PWD}" >&2
        exit 1
    }
    echo "$result"
fi
