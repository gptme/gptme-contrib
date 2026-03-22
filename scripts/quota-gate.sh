#!/usr/bin/env bash
# Quota gate for autonomous run scripts.
#
# Checks Claude Code subscription quota before starting a session.
# Exits with code 1 (skip session) if quota is exhausted; 0 if available.
#
# Usage:
#   source scripts/quota-gate.sh    # run gate; session exits if quota exhausted
#   quota_gate_check                # call function directly
#   quota_gate_check --model sonnet # check sonnet-specific quota
#
# Reads quota data from check-claude-usage.sh (cached 10 min).
# Requires: tmux, Claude Code OAuth login (run /login in CC).
# ANTHROPIC_API_KEY must NOT be set in environment.

QUOTA_GATE_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUOTA_CHECK_SCRIPT="$QUOTA_GATE_SCRIPT_DIR/check-claude-usage.sh"

# Thresholds: skip session if utilization exceeds these values
QUOTA_GATE_SESSION_THRESHOLD="${QUOTA_GATE_SESSION_THRESHOLD:-0.90}"   # 5h session window
QUOTA_GATE_WEEKLY_THRESHOLD="${QUOTA_GATE_WEEKLY_THRESHOLD:-0.90}"     # 7d weekly window

quota_gate_check() {
    # Parse args: supports both positional (quota_gate_check sonnet)
    # and flag form (quota_gate_check --model sonnet)
    local model=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --model) model="${2:-}"; shift 2 ;;
            --model=*) model="${1#--model=}"; shift ;;
            *) model="$1"; shift ;;
        esac
    done
    local log_prefix="${QUOTA_GATE_LOG_PREFIX:-[quota-gate]}"

    if [ ! -f "$QUOTA_CHECK_SCRIPT" ]; then
        echo "$log_prefix check-claude-usage.sh not found at $QUOTA_CHECK_SCRIPT, skipping quota gate" >&2
        return 0
    fi

    local quota_json
    if ! quota_json=$("$QUOTA_CHECK_SCRIPT" --json 2>/dev/null); then
        echo "$log_prefix quota check failed (non-fatal), proceeding" >&2
        return 0
    fi

    local five_hour_util seven_day_util session_ok weekly_ok
    five_hour_util=$(echo "$quota_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('five_hour',{}).get('utilization',0))" 2>/dev/null || echo "0")
    seven_day_util=$(echo "$quota_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('seven_day',{}).get('utilization',0))" 2>/dev/null || echo "0")

    # Sonnet uses a separate weekly counter
    local weekly_key="seven_day"
    if [ "$model" = "sonnet" ]; then
        weekly_key="seven_day_sonnet"
        seven_day_util=$(echo "$quota_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('seven_day_sonnet',{}).get('utilization',0))" 2>/dev/null || echo "0")
    fi

    session_ok=$(python3 -c "print('ok' if $five_hour_util < $QUOTA_GATE_SESSION_THRESHOLD else 'blocked')" 2>/dev/null || echo "ok")
    weekly_ok=$(python3 -c "print('ok' if $seven_day_util < $QUOTA_GATE_WEEKLY_THRESHOLD else 'blocked')" 2>/dev/null || echo "ok")

    if [ "$session_ok" = "blocked" ]; then
        local five_hour_resets
        five_hour_resets=$(echo "$quota_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('five_hour',{}).get('time_left','unknown'))" 2>/dev/null || echo "unknown")
        echo "$log_prefix SKIPPING — 5h session quota at $(python3 -c "print(f'{$five_hour_util:.0%}')") (threshold: $(python3 -c "print(f'{$QUOTA_GATE_SESSION_THRESHOLD:.0%}')")), resets $five_hour_resets" >&2
        return 1
    fi

    if [ "$weekly_ok" = "blocked" ]; then
        local weekly_resets
        weekly_resets=$(echo "$quota_json" | python3 -c "import json,sys; d=json.load(sys.stdin); k='$weekly_key'; print(d.get(k,{}).get('time_left','unknown'))" 2>/dev/null || echo "unknown")
        echo "$log_prefix SKIPPING — weekly quota at $(python3 -c "print(f'{$seven_day_util:.0%}')") (threshold: $(python3 -c "print(f'{$QUOTA_GATE_WEEKLY_THRESHOLD:.0%}')")), resets $weekly_resets" >&2
        return 1
    fi

    # Log pacing info if available
    local pacing_status pacing_gap
    pacing_status=$(echo "$quota_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('_pacing',{}).get('status','unknown'))" 2>/dev/null || echo "unknown")
    pacing_gap=$(echo "$quota_json" | python3 -c "import json,sys; d=json.load(sys.stdin); p=d.get('_pacing',{}); print(f\"{p.get('pace_gap',0):+.1%}\")" 2>/dev/null || echo "")

    echo "$log_prefix quota OK — 5h: $(python3 -c "print(f'{$five_hour_util:.0%}')"), weekly: $(python3 -c "print(f'{$seven_day_util:.0%}')"), pacing: $pacing_status $pacing_gap" >&2
    return 0
}

# If sourced (not executed), define function only — caller decides when to run
# If executed directly, run the gate immediately
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    quota_gate_check "$@"
fi
