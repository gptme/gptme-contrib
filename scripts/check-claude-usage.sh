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

CACHE_FILE="${CLAUDE_USAGE_CACHE_FILE:-/tmp/claude-usage-cache.json}"
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

# Dismiss the "trust this folder" prompt if present (CC v2.1.183+ shows this
# on first run from a new directory). After trust, wait for the real CC
# prompt before sending commands.
content=$(tmux capture-pane -t "$SESSION_NAME" -p 2>/dev/null || true)
if echo "$content" | grep -qi "Yes, I trust this folder"; then
    tmux send-keys -t "$SESSION_NAME" Enter
    # Wait for CC to fully initialize after trust (up to 10s)
    for _ in $(seq 1 10); do
        content=$(tmux capture-pane -t "$SESSION_NAME" -p 2>/dev/null || true)
        if echo "$content" | grep -qE '(shortcuts|Try )' && ! echo "$content" | grep -qi "Yes, I trust"; then
            break
        fi
        sleep 1
    done
fi

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
    # Stop once at least 2 window labels have been accumulated.
    # CC v2.1.183+ occasionally omits "Current week (Sonnet only)" when Sonnet
    # has no usage data, so requiring all 3 causes the loop to exhaust all
    # iterations and time out (~150s).
    _found_count=0
    echo "$ACCUM" | grep -qi 'Current session' && _found_count=$((_found_count + 1))
    echo "$ACCUM" | grep -qi 'Current week (all models)' && _found_count=$((_found_count + 1))
    echo "$ACCUM" | grep -qi 'Current week (Sonnet only)' && _found_count=$((_found_count + 1))
    if [ "$_found_count" -ge 2 ]; then
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

# Parse the TUI output and write result to cache.
# Use external Python parser (avoids heredoc escaping issues with CC v2.1.183+).
PARSER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fp="$(_creds_fingerprint)"
json_args=()
[ "$MODE" = "json" ] && json_args=(--json)
echo "$OUTPUT" | python3 "$PARSER_DIR/check-claude-usage-parser.py" \
    "${json_args[@]}" \
    --cache-file "$CACHE_FILE" \
    --cred-fingerprint "$fp"
