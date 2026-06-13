#!/usr/bin/env bash
# Check Codex CLI subscription usage/quota.
#
# Runs codex interactively in a headless tmux session, sends /status, and
# parses the TUI output to extract five-hour and weekly utilization for both
# the primary model and the Spark tier.
#
# Prerequisites:
#   1. Logged in to Codex CLI
#   2. tmux must be available
#   3. codex binary reachable via $HOME/.npm-global/bin/codex (or set CODEX_BIN)
#
# Usage:
#   ./scripts/check-codex-usage.sh          # Human-readable summary
#   ./scripts/check-codex-usage.sh --json   # JSON output for scripting
#   ./scripts/check-codex-usage.sh --raw    # Raw tmux capture for debugging
#   ./scripts/check-codex-usage.sh --no-cache --json  # Force fresh fetch
#
# Caching:
#   Results are cached to /tmp/codex-usage-cache.json (default TTL: 3 min).
#   Set CODEX_USAGE_CACHE_TTL=<seconds> to override, or use --no-cache.
#
# Normalization:
#   Codex reports "% LEFT" in its TUI. The script converts to utilization
#   (= 1 - left/100) to match the convention used by check-claude-usage.sh
#   and the shared pacing helpers.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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

CACHE_FILE="${CODEX_USAGE_CACHE_FILE:-/tmp/codex-usage-cache.json}"
CACHE_TTL="${CODEX_USAGE_CACHE_TTL:-180}"

# --- Cache check ---
if [ "$NO_CACHE" = false ] && [ -f "$CACHE_FILE" ]; then
    cache_age=$(( $(date +%s) - $(stat -c '%Y' "$CACHE_FILE" 2>/dev/null || stat -f '%m' "$CACHE_FILE" 2>/dev/null || echo 0) ))
    if [ "$cache_age" -lt "$CACHE_TTL" ]; then
        case "$MODE" in
            json) cat "$CACHE_FILE" ;;
            raw)  echo "(cached)" ;;
            *)    echo "(cached, use --json or --no-cache for fresh data)" ;;
        esac
        exit 0
    fi
fi

# --- Single-scrape concurrency guard ---
# Allow only ONE live scrape at a time; if another holds the lock, serve the
# cached result (even if stale) rather than duplicating the work.
SCRAPE_LOCK="${CODEX_USAGE_SCRAPE_LOCK:-/tmp/codex-usage-scrape.lock}"
exec 9>"$SCRAPE_LOCK"
if ! flock -n 9; then
    if [ -f "$CACHE_FILE" ]; then
        case "$MODE" in
            json) cat "$CACHE_FILE" ;;
            *)    echo "(another scrape in progress; serving cached result)" ;;
        esac
        exit 0
    fi
    echo "Error: another codex-usage scrape is already running" >&2
    exit 1
fi

# --- Dependency checks ---
if ! command -v tmux &>/dev/null; then
    echo "Error: tmux is required" >&2
    exit 1
fi

CODEX_BIN="${CODEX_BIN:-$HOME/.npm-global/bin/codex}"
if [ ! -x "$CODEX_BIN" ]; then
    echo "Error: codex binary not found at $CODEX_BIN" >&2
    exit 1
fi

# --- Pre-suppress update prompt ---
# Codex checks for updates at startup. If dismissed_version != latest_version it
# shows an interactive "Update now / Skip / Skip until next version" menu where
# pressing Enter selects "Update now" → npm install → binary replaced → exit 127.
# Setting dismissed_version = latest_version silently suppresses the prompt.
VERSION_FILE="$HOME/.codex/version.json"
if [ -f "$VERSION_FILE" ]; then
    python3 - <<'PYEOF'
import json, os
vf_path = os.path.expanduser("~/.codex/version.json")
with open(vf_path) as vf:
    data = json.load(vf)
latest = data.get("latest_version", "")
if latest and data.get("dismissed_version") != latest:
    data["dismissed_version"] = latest
    with open(vf_path, "w") as f:
        json.dump(data, f, indent=2)
PYEOF
fi

SESSION_NAME="codex-usage-$$"
TIMEOUT=45

# --- Cleanup handler ---
_cleanup() {
    tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true
}
trap _cleanup EXIT

# --- Start codex in a headless tmux session ---
tmux new-session -d -s "$SESSION_NAME" -x 200 -y 50 \
    "$CODEX_BIN --ask-for-approval never 2>&1; sleep 2"

# --- Wait for codex to become ready, dismissing any residual update prompt ---
# Readiness detection notes (codex 0.137.0):
#   - The informational "✨ Update available!" BANNER (a box) is NOT the same as
#     the interactive update MENU. The banner persists in scrollback and must be
#     IGNORED — matching it with a bare "Update available" and sending keys spams
#     keystrokes into the ready prompt forever. Only the interactive menu (with a
#     "1. Update now" option) should be dismissed; dismissed_version normally
#     suppresses it entirely, so this is purely defensive.
#   - The ready prompt is `› Write tests for @filename` (glyph at the START, ghost
#     placeholder after it) followed by a status line `gpt-5.5 high · ~/bob`. The
#     glyph is never at end-of-line, so detect readiness by the launch banner
#     `OpenAI Codex (vX.Y.Z)` or a line beginning with the prompt glyph.
_initialized=false
for _i in $(seq 1 "$TIMEOUT"); do
    content=$(tmux capture-pane -t "$SESSION_NAME" -p 2>/dev/null || true)

    # Dismiss only the INTERACTIVE update menu (not the informational banner)
    if echo "$content" | grep -qE 'Update now|Skip until next version'; then
        tmux send-keys -t "$SESSION_NAME" Down Down Enter
        sleep 2
        continue
    fi

    # Ready when the launch banner or the input prompt glyph is present
    if echo "$content" | grep -qE 'OpenAI Codex \(v|^[[:space:]]*[›>] '; then
        _initialized=true
        break
    fi

    sleep 1
done

if [ "$_initialized" = false ]; then
    echo "Error: codex failed to start within ${TIMEOUT}s" >&2
    if [ "$MODE" = "raw" ]; then
        tmux capture-pane -t "$SESSION_NAME" -p -S -200 2>/dev/null || true
    fi
    exit 1
fi

# --- Settle: the launch banner can render before the input field accepts keys.
# A short settle prevents /status being typed into a not-yet-live prompt (which
# silently drops it → "no usage data captured"). ---
sleep 3

# --- Send /status command ---
# Typing "/status" opens an autocomplete dropdown listing /status + /statusline;
# send the text first, let the dropdown render, THEN Enter to run the highlighted
# (/status) entry. Sending text+Enter together can race the dropdown.
tmux send-keys -t "$SESSION_NAME" "/status"
sleep 2
tmux send-keys -t "$SESSION_NAME" Enter
sleep 3

# --- Capture output (retry rounds for progressive rendering) ---
ACCUM=""
for _round in $(seq 1 15); do
    sleep 1
    content=$(tmux capture-pane -t "$SESSION_NAME" -p -S -150 2>/dev/null || true)
    if echo "$content" | grep -qE '[0-9]+% left'; then
        ACCUM="$content"
        # Extra wait for Spark section to render
        sleep 2
        content2=$(tmux capture-pane -t "$SESSION_NAME" -p -S -150 2>/dev/null || true)
        if echo "$content2" | grep -qE '(Spark|% left)'; then
            ACCUM="$content2"
        fi
        break
    fi
done

# --- Retry once if the first /status attempt produced nothing (input may have
# been dropped during a slow startup under load). ---
if [ -z "$ACCUM" ]; then
    tmux send-keys -t "$SESSION_NAME" "/status"
    sleep 2
    tmux send-keys -t "$SESSION_NAME" Enter
    for _round in $(seq 1 12); do
        sleep 1
        content=$(tmux capture-pane -t "$SESSION_NAME" -p -S -150 2>/dev/null || true)
        if echo "$content" | grep -qE '[0-9]+% left'; then
            ACCUM="$content"
            sleep 2
            content2=$(tmux capture-pane -t "$SESSION_NAME" -p -S -150 2>/dev/null || true)
            if echo "$content2" | grep -qE '(Spark|% left)'; then
                ACCUM="$content2"
            fi
            break
        fi
    done
fi

# --- Raw mode: dump and exit ---
if [ "$MODE" = "raw" ]; then
    echo "$ACCUM"
    exit 0
fi

# --- Validate capture ---
if [ -z "$ACCUM" ]; then
    echo "Error: no usage data captured from codex /status" >&2
    tmux capture-pane -t "$SESSION_NAME" -p -S -200 2>/dev/null >&2 || true
    exit 1
fi

# --- Parse usage via Python ---
# Normalizes "% left" → utilization (0.0–1.0, where 1.0 = fully consumed)
# Pass captured output via env var to avoid competing stdin redirections (SC2261).
JSON=$(CODEX_ACCUM="$ACCUM" REPO_ROOT="$REPO_ROOT" python3 << 'PYEOF'
import json, os, re, sys
from datetime import datetime, timezone, timedelta

output = os.environ.get("CODEX_ACCUM", "")
repo_root = os.environ["REPO_ROOT"]
# gptme_subscription.routing is the import target, but importing the package
# runs its __init__ → manager → credential_slots, so both src dirs are needed.
for _rel in (("packages", "gptme-subscription", "src"), ("packages", "credential-slots", "src")):
    _p = os.path.join(repo_root, *_rel)
    if _p not in sys.path:
        sys.path.insert(0, _p)
from gptme_subscription.routing import compute_window_pacing

def parse_limit(text, label, *, window_seconds):
    for line in text.split("\n"):
        if label.lower() in line.lower():
            m = re.search(r'(\d+)%\s+left', line)
            if not m:
                continue
            left = int(m.group(1))
            utilization = round((100 - left) / 100.0, 4)

            reset_m = re.search(r'resets\s+(.*)', line)
            reset_raw = reset_m.group(1).strip() if reset_m else ''
            resets_seconds = None
            if reset_raw:
                try:
                    now = datetime.now(timezone.utc)
                    rm = re.match(r'(\d{1,2}):(\d{2})(?:\s+on\s+(\d+)\s+(\w+))?', reset_raw)
                    if rm:
                        hour, minute = int(rm.group(1)), int(rm.group(2))
                        if rm.group(3):
                            day = int(rm.group(3))
                            month_name = rm.group(4).lower()[:3]
                            months = ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec']
                            month = months.index(month_name) + 1 if month_name in months else now.month
                            reset_dt = now.replace(month=month, day=day, hour=hour, minute=minute, second=0, microsecond=0)
                        else:
                            reset_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                            if reset_dt <= now:
                                reset_dt += timedelta(days=1)
                        resets_seconds = max(0, int((reset_dt - now).total_seconds()))
                except Exception:
                    pass

            result = {
                'utilization': utilization,
                'left_pct': left,
                'resets_in_seconds': resets_seconds,
            }
            if resets_seconds is not None:
                pacing_result = compute_window_pacing(
                    utilization,
                    resets_seconds,
                    window_seconds,
                )
                if pacing_result is not None:
                    elapsed_frac, gap, status = pacing_result
                    result.update(
                        {
                            'headroom': round(1.0 - utilization, 4),
                            'target_utilization': round(elapsed_frac, 4),
                            'pace_gap': round(gap, 4),
                            'status': status,
                        }
                    )
            return result
    return None

# Split main vs spark sections
spark_patterns = [
    'GPT-5.3-Codex-Spark', 'GPT-5.4-Codex-Spark',
    'GPT-5.5-Codex-Spark', 'Codex-Spark', 'Spark limit',
]
main_text = output
spark_text = ''
split_idx = len(output)
for pat in spark_patterns:
    m = re.search(pat, output, re.IGNORECASE)
    if m:
        split_idx = min(split_idx, m.start())
if split_idx < len(output):
    main_text = output[:split_idx]
    spark_text = output[split_idx:]

data = {
    'main': {
        'five_hour': parse_limit(main_text, '5h', window_seconds=5 * 3600),
        'weekly':    parse_limit(main_text, 'Weekly', window_seconds=7 * 24 * 3600),
    },
    'spark': {
        'five_hour': parse_limit(spark_text, '5h', window_seconds=5 * 3600),
        'weekly':    parse_limit(spark_text, 'Weekly', window_seconds=7 * 24 * 3600),
    } if spark_text else None,
    '_source': 'live',
    '_captured_at': datetime.now(timezone.utc).isoformat(),
}

print(json.dumps(data, indent=2))
PYEOF
) || true

# --- Validate and cache ---
if [ -n "$JSON" ] && echo "$JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
assert isinstance(d.get('main'), dict)
" 2>/dev/null; then
    echo "$JSON" > "$CACHE_FILE"
    case "$MODE" in
        json) echo "$JSON" ;;
        *)
            CODEX_JSON="$JSON" python3 << 'PYEOF'
import json, os
d = json.loads(os.environ["CODEX_JSON"])
print("Codex Subscription Usage")
print("=" * 60)
main = d.get("main", {})
for key, label in [("five_hour", "Session (5h)"), ("weekly", "Weekly")]:
    info = main.get(key)
    if info:
        util = info["utilization"]
        left = info["left_pct"]
        bar_width = 30
        filled = int(util * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)
        resets = info.get("resets_in_seconds")
        reset_str = f"  resets in {resets//3600}h{(resets%3600)//60}m" if resets else ""
        print(f"  {label:20s} [{bar}] {util*100:4.0f}% used ({left}% left){reset_str}")
    else:
        print(f"  {label:20s} N/A")
spark = d.get("spark") or {}
if spark.get("five_hour") or spark.get("weekly"):
    print()
    print("  Spark tier:")
    for key, label in [("five_hour", "  Session (5h)"), ("weekly", "  Weekly")]:
        info = spark.get(key)
        if info:
            util = info["utilization"]
            left = info["left_pct"]
            bar_width = 28
            filled = int(util * bar_width)
            bar = "█" * filled + "░" * (bar_width - filled)
            print(f"  {label:20s} [{bar}] {util*100:4.0f}% used ({left}% left)")
PYEOF
            ;;
    esac
else
    echo "Error: failed to parse codex usage output" >&2
    echo "Raw captured output:" >&2
    echo "$ACCUM" >&2
    exit 1
fi
