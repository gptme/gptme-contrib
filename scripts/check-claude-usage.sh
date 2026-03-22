#!/usr/bin/env bash
# Check Claude Code Max subscription usage/quota.
#
# Runs CC's /usage command in a headless tmux session and parses the TUI output.
# (Direct API calls to the usage endpoint require CC's internal auth mechanism
# which can't be replicated externally with just the OAuth token.)
#
# Prerequisites:
#   1. Logged in via `/login` in CC (OAuth token with `user:profile` scope)
#   2. ANTHROPIC_API_KEY must NOT be set in the environment
#   3. tmux must be available
#
# Usage:
#   ./scripts/check-claude-usage.sh          # Human-readable summary
#   ./scripts/check-claude-usage.sh --json   # JSON output for scripting
#   ./scripts/check-claude-usage.sh --raw    # Raw tmux capture for debugging
#   ./scripts/check-claude-usage.sh --no-cache --json  # Force fresh fetch
#
# Caching:
#   Results are cached to /tmp/claude-usage-cache.json (default TTL: 10 min).
#   The tmux/TUI approach takes ~25s, so caching avoids unnecessary overhead
#   when called from autonomous loops or monitoring scripts.
#   Set CLAUDE_USAGE_CACHE_TTL=<seconds> to override, or use --no-cache.

set -euo pipefail

# Ensure claude is in PATH (installed to ~/.local/bin but not available
# in systemd service environments which don't source the login profile)
export PATH="$HOME/.local/bin:$PATH"

# --- Parse args ---
MODE=""
NO_CACHE=false
for arg in "$@"; do
    case "$arg" in
        --json) MODE="json" ;;
        --raw) MODE="raw" ;;
        --no-cache) NO_CACHE=true ;;
        *) echo "Unknown arg: $arg" >&2; exit 1 ;;
    esac
done

CACHE_FILE="/tmp/claude-usage-cache.json"
CACHE_TTL="${CLAUDE_USAGE_CACHE_TTL:-600}"  # 10 minutes default

# --- Cache check (JSON and human-readable modes only, not --raw) ---
if [ "$MODE" != "raw" ] && [ "$NO_CACHE" = false ] && [ -f "$CACHE_FILE" ]; then
    # stat mtime: -c %Y on Linux, -f %m on macOS/BSD
    CACHE_AGE=$(( $(date +%s) - $(stat -c %Y "$CACHE_FILE" 2>/dev/null || stat -f %m "$CACHE_FILE" 2>/dev/null || echo 0) ))
    if [ "$CACHE_AGE" -lt "$CACHE_TTL" ]; then
        if [ "$MODE" = "json" ]; then
            cat "$CACHE_FILE"
            exit 0
        else
            # Render human-readable from cached JSON
            python3 -c "
import json, sys
with open('$CACHE_FILE') as f:
    result = json.load(f)
cache_age = $CACHE_AGE
print('Claude Max Subscription Usage (cached, {}s ago)'.format(cache_age))
print('=' * 60)
for key, label in [('five_hour', 'Session (5h)'), ('seven_day', 'Weekly (all)'), ('seven_day_sonnet', 'Weekly (Sonnet)')]:
    info = result.get(key)
    if info:
        util = info['utilization']
        remaining = 1 - util
        bar_width = 30
        filled = int(util * bar_width)
        bar = chr(9608) * filled + chr(9617) * (bar_width - filled)
        time_left = info.get('time_left', '')
        resets = info['resets']
        print(f'  {label:20s} [{bar}] {util*100:4.0f}% used ({remaining*100:.0f}% left)')
        print(f'  {\"\":20s} resets {resets}  ({time_left})')
    else:
        print(f'  {label:20s} N/A')
print()
"
            exit 0
        fi
    fi
fi

SESSION_NAME="claude-usage-check-$$"
TIMEOUT=25

cleanup() {
    # Try graceful exit first
    tmux send-keys -t "$SESSION_NAME" Escape 2>/dev/null || true
    sleep 0.5
    tmux send-keys -t "$SESSION_NAME" "/exit" Enter 2>/dev/null || true
    sleep 1
    tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true
}
trap cleanup EXIT

# Check prerequisites
if ! command -v tmux &>/dev/null; then
    echo "Error: tmux is required" >&2
    exit 1
fi

# If ANTHROPIC_API_KEY is in the env, CC uses API-key mode (no subscription quotas).
# The script unsets it for the CC subprocess, but warn in case something is unexpected.
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    echo "Note: ANTHROPIC_API_KEY found in env, will be unset for CC subprocess." >&2
fi

# Start CC in a headless tmux session
# Unset CLAUDECODE (nested protection) and ANTHROPIC_API_KEY (force OAuth)
tmux new-session -d -s "$SESSION_NAME" -x 120 -y 50 \
    "env -u ANTHROPIC_API_KEY -u CLAUDECODE claude 2>&1; sleep 2"

# Wait for CC to initialize
for _ in $(seq 1 "$TIMEOUT"); do
    content=$(tmux capture-pane -t "$SESSION_NAME" -p 2>/dev/null || true)
    if echo "$content" | grep -qE '(❯|shortcuts)'; then
        break
    fi
    sleep 1
done

# Send /usage command (type it, then Enter to select from autocomplete)
tmux send-keys -t "$SESSION_NAME" "/usage"
sleep 2
tmux send-keys -t "$SESSION_NAME" Enter

# Wait for usage data to render
for _ in $(seq 1 "$TIMEOUT"); do
    content=$(tmux capture-pane -t "$SESSION_NAME" -p -S -80 2>/dev/null || true)
    if echo "$content" | grep -qiE '(%\s*used|not enabled|Extra usage|Resets)'; then
        break
    fi
    sleep 1
done
sleep 1  # extra settle time for full render

# Capture output
OUTPUT=$(tmux capture-pane -t "$SESSION_NAME" -p -S -80 2>/dev/null || true)

if [ "$MODE" = "raw" ]; then
    echo "$OUTPUT"
    exit 0
fi

# Parse the TUI output
echo "$OUTPUT" | python3 -c "
import sys, re, json
from datetime import datetime, timezone, timedelta

text = sys.stdin.read()
result = {}
json_mode = '$MODE' == 'json'

# Check for errors
if 'Error:' in text and 'scope' in text.lower():
    print('Error: OAuth token missing required scope. Re-login with /login in CC.', file=sys.stderr)
    sys.exit(1)

if 'Claude API' in text and 'Max' not in text:
    print('Warning: Running in API-key mode (not Max subscription). No quota data available.', file=sys.stderr)
    sys.exit(1)

def parse_reset_time(reset_str):
    \"\"\"Parse CC's reset time string into a datetime. Returns (datetime, time_left_str).\"\"\"
    now = datetime.now(timezone.utc)
    try:
        # Normalize: strip '(UTC)', clean up
        s = reset_str.replace('(UTC)', '').strip()

        # Format: '9pm' or '9:30pm' (today or tomorrow)
        m = re.match(r'^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$', s, re.IGNORECASE)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            ampm = m.group(3).lower()
            if ampm == 'pm' and hour != 12:
                hour += 12
            elif ampm == 'am' and hour == 12:
                hour = 0
            reset_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if reset_dt <= now:
                reset_dt += timedelta(days=1)
            return reset_dt

        # Format: 'Feb 18, 8am' or 'Feb 18, 7:59am'
        m = re.match(r'^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)$', s, re.IGNORECASE)
        if m:
            month_str, day, hour, minute, ampm = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0), m.group(5).lower()
            if ampm == 'pm' and hour != 12:
                hour += 12
            elif ampm == 'am' and hour == 12:
                hour = 0
            # Parse month name
            months = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}
            month = months.get(month_str[:3].lower(), now.month)
            year = now.year
            reset_dt = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
            if reset_dt < now:
                reset_dt = reset_dt.replace(year=year + 1)
            return reset_dt

    except Exception:
        pass
    return None

def format_time_left(reset_dt):
    \"\"\"Format a human-readable time-left string from a reset datetime.\"\"\"
    if not reset_dt:
        return ''
    now = datetime.now(timezone.utc)
    delta = reset_dt - now
    total_sec = delta.total_seconds()
    if total_sec <= 0:
        return '(resetting now)'
    hours = total_sec / 3600
    if hours >= 48:
        return f'{hours / 24:.1f}d left'
    elif hours >= 1:
        h = int(hours)
        m = int((hours - h) * 60)
        return f'{h}h{m:02d}m left'
    else:
        return f'{int(total_sec / 60)}m left'

# Generic parser: find label line, then scan next few lines for '% used' and 'Resets'
labels = [
    ('Current session', 'five_hour'),
    ('Current week (all models)', 'seven_day'),
    ('Current week (Sonnet only)', 'seven_day_sonnet'),
]
lines = text.split('\n')
for label_text, key in labels:
    for i, line in enumerate(lines):
        if label_text in line:
            # Search the next 3 lines for '% used' and 'Resets'
            chunk = '\n'.join(lines[i:i+4])
            pct_m = re.search(r'(\d+)%\s*used', chunk)
            reset_m = re.search(r'Resets\s+(.+)', chunk)
            if pct_m:
                result[key] = {
                    'utilization': int(pct_m.group(1)) / 100,
                    'resets': reset_m.group(1).strip() if reset_m else 'unknown',
                }
            break

if not result:
    print('Error: Could not parse usage data from CC output.', file=sys.stderr)
    print('Run with --raw to see raw output.', file=sys.stderr)
    sys.exit(1)

# Add time_left to JSON output
for key in result:
    reset_dt = parse_reset_time(result[key]['resets'])
    if reset_dt:
        delta = reset_dt - datetime.now(timezone.utc)
        result[key]['resets_in_seconds'] = max(0, int(delta.total_seconds()))
        result[key]['time_left'] = format_time_left(reset_dt)

# --- Off-peak detection ---
# Peak: 8 AM-2 PM ET (12:00-18:00 UTC) weekdays. Off-peak: everything else.
# Anthropic may run promotions where off-peak usage is discounted or doesn't count
# against weekly limits. Set CLAUDE_USAGE_PROMO_START / CLAUDE_USAGE_PROMO_END
# (ISO 8601 UTC strings) to enable off-peak tracking for a custom promo window.
import os as _os
now_utc = datetime.now(timezone.utc)
is_weekday = now_utc.weekday() < 5
is_peak_hour = is_weekday and 12 <= now_utc.hour < 18
promo_start_env = _os.environ.get('CLAUDE_USAGE_PROMO_START', '')
promo_end_env = _os.environ.get('CLAUDE_USAGE_PROMO_END', '')
if promo_start_env and promo_end_env:
    promo_start = datetime.fromisoformat(promo_start_env)
    promo_end = datetime.fromisoformat(promo_end_env)
    promo_active = promo_start <= now_utc <= promo_end
else:
    promo_active = False
off_peak = promo_active and not is_peak_hour
result['_off_peak'] = {
    'active': off_peak,
    'promo_active': promo_active,
    'is_peak_hour': is_peak_hour,
    'peak_hours_utc': '12:00-18:00 weekdays',
}

# --- Weekly pacing: target ~90% utilization by week end ---
seven_day = result.get('seven_day', {})
if seven_day and seven_day.get('resets_in_seconds') is not None:
    total_window = 7 * 24 * 3600
    remaining_secs = seven_day['resets_in_seconds']
    elapsed_frac = max(0, 1.0 - remaining_secs / total_window)
    target = elapsed_frac * 0.9
    actual = seven_day.get('utilization', 0)
    gap = target - actual
    result['_pacing'] = {
        'elapsed_fraction': round(elapsed_frac, 3),
        'target_utilization': round(target, 3),
        'actual_utilization': round(actual, 3),
        'pace_gap': round(gap, 3),
        'status': 'underusing' if gap > 0.05 else ('overusing' if gap < -0.05 else 'on_track'),
    }

# Write cache file (always, for both modes)
cache_path = '$CACHE_FILE'
try:
    with open(cache_path, 'w') as f:
        json.dump(result, f, indent=2)
except OSError:
    pass

if json_mode:
    print(json.dumps(result, indent=2))
else:
    print('Claude Max Subscription Usage')
    print('=' * 60)
    for key, label in [('five_hour', 'Session (5h)'), ('seven_day', 'Weekly (all)'), ('seven_day_sonnet', 'Weekly (Sonnet)')]:
        info = result.get(key)
        if info:
            util = info['utilization']
            remaining = 1 - util
            bar_width = 30
            filled = int(util * bar_width)
            bar = '█' * filled + '░' * (bar_width - filled)
            time_left = info.get('time_left', '')
            resets = info['resets']
            print(f'  {label:20s} [{bar}] {util*100:4.0f}% used ({remaining*100:.0f}% left)')
            print(f'  {\"\":20s} resets {resets}  ({time_left})')
        else:
            print(f'  {label:20s} N/A')
    print()
"
