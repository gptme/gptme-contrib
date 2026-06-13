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
#
# JSON contract (--json):
#   The CC v2.1.175 /usage TUI exposes three categories. They are emitted under
#   BOTH stable keys (consumed by quota-gate.sh / autonomous-run-cc.sh) and new
#   descriptive aliases (same dict object):
#     five_hour        == current_session       (Current session)
#     seven_day        == current_week_all       (Current week, all models) ← weekly Max limit
#     seven_day_sonnet == current_week_sonnet     (Current week, Sonnet only)
#   Each category dict has:
#     utilization        float 0..1   (fraction of the limit used)
#     resets             str          (raw reset string, e.g. 'Jun 16, 3:59pm')
#     resets_in_seconds  int          (seconds until reset)
#     time_left          str          (e.g. '3.8d left')
#     model              str          (human label)
#   Plus top-level: _pacing {pace_gap, status, ...}, _off_peak {...},
#   _cred_fingerprint, and (on fallback) _source / _warning.
#   Consumers: quota-gate.sh reads five_hour.utilization, seven_day.utilization,
#   seven_day_sonnet.utilization, *.time_left; autonomous-run-cc.sh reads
#   _pacing.pace_gap.

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

CACHE_FILE="${CLAUDE_USAGE_CACHE_FILE:-/tmp/claude-usage-cache.json}"
FALLBACK_CACHE_FILE="${CLAUDE_USAGE_FALLBACK_CACHE_FILE:-/tmp/claude-usage-stale-fallback.json}"
CACHE_TTL="${CLAUDE_USAGE_CACHE_TTL:-600}"

# Determine credential file for fingerprinting.
# NOTE: the real file is `.credentials.json` (dot-prefixed) — the old default
# `credentials.json` (no dot) never existed, so the fingerprint was always the
# missing-file sentinel and the cache was ALWAYS judged invalid → every periodic
# caller did a full ~1-core /usage scrape instead of reusing the 10-min cache.
CREDS_FILE="${CLAUDE_USAGE_CREDS_FILE:-$HOME/.claude/.credentials.json}"

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
        for key, label in [
            ('five_hour', 'Current session'),
            ('seven_day', 'Current week (all models)'),
            ('seven_day_sonnet', 'Current week (Sonnet only)'),
        ]:
            info = cached.get(key)
            if info and isinstance(info, dict):
                util = info.get('utilization', 0)
                resets = info.get('resets', 'unknown')
                _r = resets.replace('(UTC)', '').strip()
                resets_disp = resets if resets in ('unknown', 'session_end') else f'{_r} UTC'
                time_left = info.get('time_left', '')
                tl = f' ({time_left})' if time_left else ''
                print(f'  {label}: {util*100:.0f}% used — resets {resets_disp}{tl}')
            else:
                print(f'  {label}: N/A')
        print()
    sys.exit(0)
sys.exit(1)
" && exit 0
fi

# --- Single-scrape concurrency guard (thundering-herd protection) ---
# Multiple periodic callers (bob-vitals, subscription-check, the telemetry
# exporter) can all miss the cache in the same window and each launch a full
# ~60-140s /usage scrape. Each scrape is a heavy claude TUI; piled up they
# starve a small box (observed 2026-06-08: 12 concurrent on a 3-core VM, CPU
# pressure ~70%, sessions timing out). Allow only ONE live scrape at a time; if
# another holds the lock, serve the most recent cache (even if stale) rather
# than duplicating the work. fd 9 stays held for the rest of the script and is
# released automatically on exit.
SCRAPE_LOCK="${CLAUDE_USAGE_SCRAPE_LOCK:-/tmp/claude-usage-scrape.lock}"
# flock is Linux-only (util-linux) and absent on macOS. Guard is a no-op there;
# concurrent scrapes on macOS are benign (no multi-service automated setup).
# --no-cache explicitly requests a fresh scrape so we skip the guard to avoid
# silently handing the caller stale data (the documented contract is "Force fresh fetch").
# --raw is excluded for the same reason the TTL cache check above excludes it:
# raw callers want the unformatted scrape output, and the lock-held fallback only
# emits json/human-readable cache — serving that to a raw caller would silently
# hand back formatted data instead of raw.
if [ "$MODE" != "raw" ] && [ "$NO_CACHE" = false ] && command -v flock >/dev/null 2>&1; then
    exec 9>"$SCRAPE_LOCK"
    if ! flock -n 9; then
        # Another scrape is running — serve the most recent cache (even if stale)
        # rather than queuing up.
        if [ -f "$CACHE_FILE" ]; then
            fp="$(_creds_fingerprint)"
            python3 -c "
import json, sys
with open('$CACHE_FILE') as f:
    cached = json.load(f)
if cached.get('_cred_fingerprint', '') == '$fp':
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
else:
    sys.exit(1)  # cred mismatch
" && exit 0
            # Cache exists but credential fingerprint does not match the current slot.
            echo "Warning: a usage scrape is already running; cached data belongs to a different credential slot." >&2
            exit 1
        fi
        # No cache at all — warn but exit cleanly (don't pile on).
        echo "Warning: a usage scrape is already running and no cache is available." >&2
        exit 0
    fi
fi  # command -v flock

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

# Wait for the /usage TUI to load (CC v2.1.175 tab-based layout).
# The new layout shows the Session + both weekly categories at once under the
# 'Usage' tab, so we wait for the category labels or the footer to appear.
for _ in $(seq 1 "$TIMEOUT"); do
    content=$(tmux capture-pane -t "$SESSION_NAME" -p -S -80 2>/dev/null || true)
    if echo "$content" | grep -qiE '(Esc to cancel|d to day|w to week|Current week|% used)'; then
        break
    fi
    sleep 1
done
# Capture the "Current session / Current week" usage bars.
#
# Quirk (CC v2.1.168): the usage bars render *alongside* a "Scanning local
# sessions…" pass that runs continuously and periodically redraws the pane,
# briefly wiping the bars — so they visibly blink. Worse, the three windows
# (Current session = 5h, Current week (all models) = 7d, Current week (Sonnet
# only) = 7d Sonnet) paint progressively, so a single capture often catches only
# the first one or two before the redraw (the intermittent "quota bars don't
# show" / "Sonnet window missing" bug). No single fixed delay reliably catches
# all three.
#
# Robust approach: tab away and back (Usage -> Stats -> Usage) to force fresh
# renders, sample the pane many times, and ACCUMULATE every frame that contains
# a bar into one buffer. The parser takes the last occurrence of each window
# (later frames are more fully rendered), so all three are assembled across
# frames even when no single frame has all of them. Stop early once all three
# labels have been seen.
# Do NOT use the 'w' week toggle — it can retrigger the scan.
OUTPUT=""
ACCUM=""
for _outer in $(seq 1 12); do
    tmux send-keys -t "$SESSION_NAME" Right   # away to Stats
    sleep 0.5
    tmux send-keys -t "$SESSION_NAME" Left    # back to Usage -> forces fresh render
    sleep 0.8
    for _inner in $(seq 1 8); do
        content=$(tmux capture-pane -t "$SESSION_NAME" -p -S -80 2>/dev/null || true)
        if echo "$content" | grep -qE '[0-9]+% used'; then
            ACCUM="${ACCUM}"$'\n'"${content}"
        fi
        if echo "$content" | grep -qi 'Nothing over 10%'; then
            ACCUM="${ACCUM}"$'\n'"${content}"
            break
        fi
        sleep 0.3
    done
    # Stop once all three window labels have been accumulated.
    if echo "$ACCUM" | grep -qi 'Current session' \
        && echo "$ACCUM" | grep -qi 'Current week (all models)' \
        && echo "$ACCUM" | grep -qi 'Current week (Sonnet only)'; then
        break
    fi
    if echo "$ACCUM" | grep -qi 'Nothing over 10%'; then
        break
    fi
done
OUTPUT="$ACCUM"
# Fall back to whatever is on screen if no bars were ever captured.
[ -z "$OUTPUT" ] && OUTPUT=$(tmux capture-pane -t "$SESSION_NAME" -p -S -80 2>/dev/null || true)

if [ "$MODE" = "raw" ]; then
    echo "$OUTPUT"
    exit 0
fi

# The "Scanning local sessions…" line coexists with the rendered bars (it is a
# continuous background pass), so it is NOT on its own an error. Only warn when
# we captured no usage bars at all — that's the genuine stuck/auth-failure case.
if ! echo "$OUTPUT" | grep -qE '[0-9]+% used' \
    && echo "$OUTPUT" | grep -qi 'Scanning local sessions'; then
    echo "Warning: CC /usage produced no usage bars (still scanning / likely auth failure). Using fallback data." >&2
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

# --- Parser for CC v2.1.175 TUI ---
# The /usage TUI now shows three usage categories as BLOCKS of three lines each,
# all visible at once under the 'Usage' tab (no day/week toggle needed):
#
#   Current session
#   ███▌                                               7% used
#   Resets 11:29am (UTC)
#
#   Current week (all models)
#   █▌                                                 3% used
#   Resets Jun 16, 3:59pm (UTC)
#
#   Current week (Sonnet only)
#   ▌                                                  1% used
#   Resets Jun 16, 4pm (UTC)
#
# A category is: a label line, followed (within a few lines) by a bar+'N% used'
# line, followed by a 'Resets ...' line.
#
# We map the three categories onto the established JSON keys (kept stable for
# downstream consumers like quota-gate.sh and autonomous-run-cc.sh):
#   Current session            -> five_hour
#   Current week (all models)  -> seven_day
#   Current week (Sonnet only) -> seven_day_sonnet

lines = text.split('\\n')

# Category label -> (json key, human label). Matched as a substring (case-insensitive).
category_defs = [
    ('current session',            'five_hour',        'Current session'),
    ('current week (all models)',  'seven_day',        'Current week (all models)'),
    ('current week (sonnet',       'seven_day_sonnet', 'Current week (Sonnet only)'),
]

pct_re = re.compile(r'(\d+)%\s*used')
reset_re = re.compile(r'Resets\s+(.+)')

def find_category(label_match):
    \"\"\"Find the pct and reset string for the category whose label line contains
    label_match. Scans the lines after the label until the next category label,
    picking up the first '% used' and 'Resets' line.\"\"\"
    other_labels = [m for (m, _k, _l) in category_defs if m != label_match]
    for i, line in enumerate(lines):
        low = line.strip().lower()
        if label_match in low:
            pct = None
            reset_str = None
            # Look ahead a small window for the bar/pct line and the resets line.
            for j in range(i + 1, min(i + 6, len(lines))):
                nxt = lines[j].strip()
                nxt_low = nxt.lower()
                # Stop if we hit the next category label.
                if any(ol in nxt_low for ol in other_labels):
                    break
                if pct is None:
                    pm = pct_re.search(nxt)
                    if pm:
                        pct = int(pm.group(1)) / 100
                rm = reset_re.search(nxt)
                if rm:
                    reset_str = rm.group(1).strip()
                if pct is not None and reset_str is not None:
                    break
            if pct is not None:
                return pct, reset_str
    return None, None

for label_match, key, label in category_defs:
    pct, reset_str = find_category(label_match)
    if pct is not None:
        result[key] = {
            'model': label,
            'utilization': pct,
            'resets': reset_str if reset_str else 'unknown',
        }

# If nothing parsed, fall back (auth failure, scanning state, or TUI changed again).
if not result:
    low_usage = False  # the old 'Nothing over 10%' empty state no longer exists
    session_cost = None
    if low_usage:
        # No usage to report — set all to 0
        now = datetime.now(timezone.utc)
        # Estimate next weekly reset (Thu 00:00 UTC = Wed-night / Thu-morning)
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

# Add new-name aliases for the three categories (CC v2.1.175 names), pointing at
# the SAME dicts as the stable keys. Consumers may read either name.
#   five_hour        <-> current_session
#   seven_day        <-> current_week_all
#   seven_day_sonnet <-> current_week_sonnet
for _old, _new in [
    ('five_hour', 'current_session'),
    ('seven_day', 'current_week_all'),
    ('seven_day_sonnet', 'current_week_sonnet'),
]:
    if _old in result and isinstance(result[_old], dict):
        result[_new] = result[_old]

# Stamp the credential fingerprint into the cache so a later cache read can
# detect a credential switch (different slot or rewritten slot) and bypass.
# Format: '<resolved-target-inode>:<resolved-target-mtime>' (mtime as int).
# See _creds_fingerprint() in the surrounding bash for the matching reader.
try:
    _st = _os.stat('$CREDS_FILE')  # follows symlinks by default
    result['_cred_fingerprint'] = f'{_st.st_ino}:{int(_st.st_mtime)}'
except OSError:
    # Must match the bash _creds_fingerprint() missing-file sentinel ('0:0'),
    # not '' — otherwise the cache-read fp comparison never matches and the
    # cache is permanently bypassed (every caller re-scrapes).
    result['_cred_fingerprint'] = '0:0'

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
    # Only save fallback when at least 2 of the 3 live windows have data
    # above noise threshold, to avoid persisting a partially-captured state.
    _live_windows = sum(
        1 for k in ('five_hour', 'seven_day', 'seven_day_sonnet')
        if isinstance(result.get(k), dict)
        and result[k].get('utilization', 0) > 0.05
    )
    if (
        _max_util > 0.05
        and result.get('_source') not in ('fallback_cache', 'subscription-reset-times')
        and _live_windows >= 2
    ):
        with open('$FALLBACK_CACHE_FILE', 'w') as _f:
            json.dump(result, _f, indent=2)
except OSError:
    pass

if json_mode:
    print(json.dumps(result, indent=2))
else:
    print('Claude Max Subscription Usage')
    print('=' * 60)
    for key, label in [
        ('five_hour', 'Current session'),
        ('seven_day', 'Current week (all models)'),
        ('seven_day_sonnet', 'Current week (Sonnet only)'),
    ]:
        info = result.get(key)
        if info and isinstance(info, dict):
            util = info['utilization']
            resets = info.get('resets', 'unknown')
            _r = resets.replace('(UTC)', '').strip()
            resets_disp = resets if resets in ('unknown', 'session_end') else f'{_r} UTC'
            time_left = info.get('time_left', '')
            tl = f' ({time_left})' if time_left else ''
            print(f'  {label}: {util*100:.0f}% used — resets {resets_disp}{tl}')
        else:
            print(f'  {label}: N/A')
    print()
"
