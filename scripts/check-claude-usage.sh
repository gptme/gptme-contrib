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
        --json|--jsonl) MODE="json" ;;
        --raw) MODE="raw" ;;
        --no-cache) NO_CACHE=true ;;
    esac
done

CACHE_FILE="/tmp/claude-usage-cache.json"
FALLBACK_CACHE_FILE="/tmp/claude-usage-stale-fallback.json"
CACHE_TTL="${CLAUDE_USAGE_CACHE_TTL:-600}"

# Determine credential file for fingerprinting
CREDS_FILE="$HOME/.claude/credentials.json"

_creds_fingerprint() {
    # Emit a cache-busting fingerprint for the current credential slot.
    # Resolve symlinks so that a live slot switch (ln -sfn) changes the fingerprint.
    local resolved
    resolved=$(readlink -f "$CREDS_FILE" 2>/dev/null || echo "$CREDS_FILE")
    if [ -f "$resolved" ]; then
        stat -c '%i:%Y' "$resolved" 2>/dev/null || stat -f '%i:%m' "$resolved" 2>/dev/null || echo '0:0'
    else
        echo "0:0"
    fi
}

# --- Cache check (JSON and human-readable modes only, not --raw) ---
if [ "$MODE" != "raw" ] && [ "$NO_CACHE" = false ] && [ -f "$CACHE_FILE" ]; then
    # Check cache freshness: mtime <= CACHE_TTL seconds ago
    cache_mtime=$(stat -c '%Y' "$CACHE_FILE" 2>/dev/null || stat -f '%m' "$CACHE_FILE" 2>/dev/null || echo 0)
    now_epoch=$(date +%s)
    cache_age=$((now_epoch - cache_mtime))
    fp="$(_creds_fingerprint)"

    python3 -c "
import json, sys
with open('$CACHE_FILE') as f:
    cached = json.load(f)
# Cache is valid if: fresh enough AND fingerprint matches current credential
fresh = $cache_age < $CACHE_TTL
fp_match = cached.get('_cred_fingerprint', '') == '$fp'
if fresh and fp_match:
    if '$MODE' == 'json':
        print(json.dumps(cached, indent=2))
    else:
        print('Claude Max Subscription Usage')
        print('=' * 60)
        for key, label in [('five_hour', 'Session (5h)'), ('seven_day', 'Weekly (all)'), ('seven_day_sonnet', 'Weekly (Sonnet)')]:
            info = cached.get(key)
            if info and isinstance(info, dict):
                util = info.get('utilization', 0)
                remaining = 1 - util
                bar_width = 30
                filled = int(util * bar_width)
                bar = '█' * filled + '░' * (bar_width - filled)
                time_left = info.get('time_left', '')
                resets = info.get('resets', 'unknown')
                print(f'  {label:20s} [{bar}] {util*100:4.0f}% used ({remaining*100:.0f}% left)')
                print(f'  {\"\":20s} resets {resets}  ({time_left})')
            else:
                print(f'  {label:20s} N/A')
        print()
    sys.exit(0)
sys.exit(1)
" && exit 0
fi

SESSION_NAME="claude-usage-check-$$"
TIMEOUT=25

# Reap stale sessions from prior runs (SIGKILLed, systemd-terminated, etc.).
# Any claude-usage-check-* session older than 10 minutes is stale (the script
# runs with TIMEOUT=25 and usually completes in under a minute).
reap_stale_sessions() {
    local now stale_cutoff
    now=$(date +%s)
    stale_cutoff=$((now - 600))  # 10 minutes ago
    while IFS='|' read -r sess created; do
        [ -z "$sess" ] && continue
        [ -z "$created" ] && continue
        if [ "$created" -lt "$stale_cutoff" ]; then
            tmux kill-session -t "$sess" 2>/dev/null || true
        fi
    done < <(tmux list-sessions -F '#{session_name}|#{session_created}' 2>/dev/null \
        | grep '^claude-usage-check-' || true)
}
reap_stale_sessions

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

# Wait for the /usage TUI to load (CC v2.1.168 tab-based layout)
for _ in $(seq 1 "$TIMEOUT"); do
    content=$(tmux capture-pane -t "$SESSION_NAME" -p -S -80 2>/dev/null || true)
    if echo "$content" | grep -qiE '(Esc to cancel|d to day|w to week|Nothing over 10%)'; then
        break
    fi
    sleep 1
done
sleep 1  # extra settle time for full render

# If the TUI is still loading ("Scanning local sessions"), wait longer
for _ in $(seq 1 10); do
    content=$(tmux capture-pane -t "$SESSION_NAME" -p -S -80 2>/dev/null || true)
    if ! echo "$content" | grep -qi 'Scanning local sessions'; then
        break
    fi
    sleep 2
done

# Switch to week view (default is day view, we need weekly for monitoring)
tmux send-keys -t "$SESSION_NAME" "w"
sleep 3

# Capture output (after week toggle)
OUTPUT=$(tmux capture-pane -t "$SESSION_NAME" -p -S -80 2>/dev/null || true)

if [ "$MODE" = "raw" ]; then
    echo "$OUTPUT"
    exit 0
fi

# Check if still scanning (auth failure — token revoked or network issue)
if echo "$OUTPUT" | grep -qi 'Scanning local sessions'; then
    echo "Warning: CC /usage still scanning (likely auth failure). Using fallback data." >&2
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

# --- Parser for CC v2.1.168+ TUI ---
# The new /usage TUI shows per-model usage in a section under 'Usage' tab.
# Format examples:
#   Last 24h / This week · these are independent characteristics...
#   Opus              ████████░░░░░░░░░░  86% used · Resets Thu, 9pm
#   Opus              ████████████████████  100% used · Resets ...
#   Claude 3.5 Sonnet ██████░░░░░░░░░░░░░░  55% used · Resets ...
# Or when usage is very low:
#   Nothing over 10% in this period — try the other window.
#
# In the 'Session' section above the usage section, we also find:
#   Total cost, duration, Usage: input/output tokens
# The 5-hour session utilization is estimated from the session usage data.

lines = text.split('\\n')

# First check for the 'Nothing over 10%' empty state
low_usage = 'Nothing over 10%' in text

# --- Strategy: find usage bars per model ---
# Look for lines that contain a model name, a usage bar (unicode blocks),
# a percentage, and optionally 'Resets'.
# Common model names: Opus, Sonnet, Claude 3.5, Haiku, Claude 4, etc.

# Also extract session-level data from the Session section
session_cost = None
session_usage_total = 0  # Will estimate 5h utilization from this

# First pass: find the 'Session' section and extract its data
in_session = False
for line in lines:
    stripped = line.strip()
    if stripped == 'Session':
        in_session = True
        continue
    if stripped and not stripped.startswith('Total') and not stripped.startswith('Usage:') and not stripped.startswith('  '):
        # Check if this line is outside Session section
        if not stripped.startswith('▔') and stripped not in ('', ' '):
            in_session = False
            continue
    if in_session:
        # Total cost:            $0.0000
        cm = re.search(r'Total cost:\s+[$]?([\d.]+)', stripped)
        if cm:
            session_cost = float(cm.group(1))
            continue
        # Usage:                 0 input, 0 output, 0 cache read, 0 cache write
        um = re.search(r'Usage:\s+(\d+)', stripped)
        if um:
            session_usage_total += int(um.group(1))

# --- Model usage extraction ---
# Lines in the usage section look like:
#   ModelName   ████░░  N% used · Resets <time>
# The bar is made of unicode block chars (▀▁▂▃▄▅▆▇█░▒▓)

# Collect all lines that might be model usage rows
model_lines = []
for line in lines:
    stripped = line.strip()
    # A model usage line contains a percentage and likely a bar character
    has_pct = re.search(r'(\d+)%\s*used', stripped)
    has_bar = bool(re.search(r'[█░▒▓▀▁▂▃▄▅▆▇▉▊▋▌▍▎▏]', stripped))
    has_resets = 'Resets' in stripped
    if has_pct and (has_bar or has_resets):
        model_lines.append(stripped)

# Parse each model line
for line in model_lines:
    # Match: model name, percentage, reset time
    # Model names are at the start of the line, before the bar or percentage
    # Examples:
    # 'Opus              ████████░░░░░░░░░░  86% used · Resets Thu, 9pm'
    # 'Claude 3.5 Sonnet ██████░░░░░░░░░░░░░░  55% used · Resets ...'
    # 'Opus              ████████████████████  100% used · Resets ...'

    pct_m = re.search(r'(\d+)%\s*used', line)
    reset_m = re.search(r'Resets\s+(.+)', line)
    if not pct_m:
        continue

    pct = int(pct_m.group(1)) / 100
    reset_str = reset_m.group(1).strip() if reset_m else None

    # Identify the model name (text before the bar or percentage)
    # Strip the bar characters and percentage to extract model name
    # Pattern: <model_name> <optional bar> <percentage>
    stripped_line = re.sub(r'[█░▒▓▀▁▂▃▄▅▆▇▉▊▋▌▍▎▏\s]+', ' ', line)
    # Now extract text before '%'
    pct_idx = stripped_line.find('%')
    if pct_idx > 0:
        # Get text before the percentage
        before_pct = stripped_line[:pct_idx].strip()
        # Remove the number
        before_pct = re.sub(r'\d+\s*$', '', before_pct).strip()
        model_name = before_pct
    else:
        model_name = 'unknown'

    # Skip very short names (noise)
    if len(model_name) < 2:
        continue

    # Map model names to keys
    name_lower = model_name.lower()
    if 'sonnet' in name_lower:
        key = 'seven_day_sonnet'
        label = model_name
    else:
        # Opus and any other model → weekly Opus/general quota
        key = 'seven_day'
        label = model_name

    result[key] = {
        'model': label,
        'utilization': pct,
        'resets': reset_str if reset_str else 'unknown',
    }


# If no model lines found but we detected low usage, return zeros
if not result:
    if low_usage:
        # No usage to report — set all to 0
        now = datetime.now(timezone.utc)
        # Estimate next weekly reset (Thu 00:00 UTC = "Wed night / Thu morning")
        days_until_thu = (3 - now.weekday()) % 7  # Mon=0, Thu=3
        if days_until_thu == 0:  # today IS Thursday; midnight already passed
            days_until_thu = 7
        weekly_reset = now + timedelta(days=days_until_thu)
        weekly_reset = weekly_reset.replace(hour=0, minute=0, second=0, microsecond=0)
        weekly_secs = max(0, int((weekly_reset - now).total_seconds()))
        weekly_reset_str = weekly_reset.strftime('%a, %-I%p')

        # 5h window reset is ~5h from now (sliding, but approximate)
        five_hour_reset = now + timedelta(hours=5)
        five_hour_secs = int((five_hour_reset - now).total_seconds())
        five_hour_reset_str = five_hour_reset.strftime('%-I:%M%p').lower()

        result['five_hour'] = {
            'utilization': 0.0,
            'resets': five_hour_reset_str,
            'resets_in_seconds': five_hour_secs,
            'time_left': format_time_left(five_hour_reset),
        }
        result['seven_day'] = {
            'utilization': 0.0,
            'resets': weekly_reset_str,
            'resets_in_seconds': weekly_secs,
            'time_left': format_time_left(weekly_reset),
        }
        result['seven_day_sonnet'] = {
            'utilization': 0.0,
            'resets': weekly_reset_str,
            'resets_in_seconds': weekly_secs,
            'time_left': format_time_left(weekly_reset),
        }
    else:
        # No model lines found and no low-usage signal.
        # This is the CC v2.1.168+ characteristics-only format where the
        # /usage TUI shows usage breakdowns (50% of usage was while 4+
        # sessions) but no model-level utilization percentages.
        # Try fallback chain before giving up.
        for _attempt_path in ['$FALLBACK_CACHE_FILE']:
            try:
                with open(_attempt_path) as _f:
                    fb = json.load(_f)
                # Try FALLBACK_CACHE format first (has seven_day, five_hour keys)
                if isinstance(fb, dict) and any(
                    _k in fb for _k in ('five_hour', 'seven_day', 'seven_day_sonnet')
                ):
                    result = fb
                    result['_source'] = 'fallback_cache'
                    result['_warning'] = 'CC v2.1.168+ TUI no longer shows utilization percentages \u2014 using cached values (may be stale)'
                    print('Warning: using fallback cache (CC v2.1.168 TUI does not show quota data).', file=sys.stderr)
                    break
                # Try subscription-reset-times format (slot keys like 'bob' or any username)
                _bt = next(
                    (v for v in fb.values()
                     if isinstance(v, dict) and any(k in v for k in ('weekly_utilization', 'five_hour_utilization'))),
                    None,
                )
                if _bt is not None:
                    _wu = _bt.get('weekly_utilization', 0)
                    _fh = _bt.get('five_hour_utilization', 0)
                    _su = _bt.get('sonnet_weekly_utilization', 0)
                    result = {
                        'seven_day': {'utilization': float(_wu), 'resets': 'unknown'},
                        'five_hour': {'utilization': float(_fh), 'resets': 'unknown'},
                        'seven_day_sonnet': {'utilization': float(_su), 'resets': 'unknown'},
                        '_source': 'subscription-reset-times',
                        '_warning': 'CC v2.1.168 TUI does not show quota data \u2014 using stale values from subscription-reset-times.json',
                    }
                    print('Warning: using subscription-reset-times fallback (CC v2.1.168 TUI issue).', file=sys.stderr)
                    break
            except (OSError, json.JSONDecodeError):
                continue
        if not result:
            print('Error: Could not parse usage data, and no fallback available.', file=sys.stderr)
            print('Run with --raw to see raw output.', file=sys.stderr)
            sys.exit(1)

# Estimate five_hour (session) utilization from session cost data.
# In CC Max billing, the 5-hour window cost is capped. We estimate utilization
# as a fraction of the session cost / cap, or use the session usage data.
if 'five_hour' not in result:
    # Estimate based on what we know from the session section
    # Claude Max session cap is ~\$15-20 per session. If session_cost is available
    # and non-zero, calculate utilization as proportion.
    if session_cost is not None and session_cost > 0:
        # Assume ~\$15 per session cap (approximate for Claude Max)
        est_cap = 15.0
        est_util = min(1.0, session_cost / est_cap)
        result['five_hour'] = {
            'utilization': round(est_util, 2),
            'resets': 'session_end',
        }
    elif low_usage:
        # Already handled above, but double-check
        pass

# Add time_left and resets_in_seconds to all entries
for key in list(result.keys()):
    info = result[key]
    if not isinstance(info, dict):
        continue
    reset_str = info.get('resets', '')
    if reset_str and reset_str not in ('unknown', 'session_end'):
        reset_dt = parse_reset_time(reset_str)
        if reset_dt:
            delta = reset_dt - datetime.now(timezone.utc)
            info['resets_in_seconds'] = max(0, int(delta.total_seconds()))
            info['time_left'] = format_time_left(reset_dt)
    elif 'resets_in_seconds' not in info:
        info['resets_in_seconds'] = 0
        info['time_left'] = ''

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

# Stamp the credential fingerprint into the cache so a later cache read can
# detect a credential switch (different slot or rewritten slot) and bypass.
# Format: '<resolved-target-inode>:<resolved-target-mtime>' (mtime as int).
# See _creds_fingerprint() in the surrounding bash for the matching reader.
try:
    _st = _os.stat('$CREDS_FILE')  # follows symlinks by default
    result['_cred_fingerprint'] = f'{_st.st_ino}:{int(_st.st_mtime)}'
except OSError:
    result['_cred_fingerprint'] = ''

# Write cache file (always, for both modes)
cache_path = '$CACHE_FILE'
try:
    with open(cache_path, 'w') as f:
        json.dump(result, f, indent=2)
except OSError:
    pass

# Persist the last successful result as a fallback for when auth breaks
# or the TUI format changes (CC v2.1.168+ characteristics-only format).
# Only save when we have REAL utilization data (>5%) to avoid clobbering
# the fallback with zeros during an auth outage.
try:
    _seven = result.get('seven_day', {})
    _five = result.get('five_hour', {})
    _sonnet = result.get('seven_day_sonnet', {})
    _max_util = max(
        (_seven.get('utilization', 0) if isinstance(_seven, dict) else 0),
        (_five.get('utilization', 0) if isinstance(_five, dict) else 0),
        (_sonnet.get('utilization', 0) if isinstance(_sonnet, dict) else 0),
    )
    if _max_util > 0.05 and result.get('_source') not in ('fallback_cache', 'subscription-reset-times'):
        with open('$FALLBACK_CACHE_FILE', 'w') as _f:
            json.dump(result, _f, indent=2)
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
