#!/usr/bin/env bash
# shellcheck disable=SC2140,SC2154
# Check OpenRouter API key usage and limits.
#
# Uses the /api/v1/auth/key endpoint — proper API, no tmux scraping needed.
#
# Usage:
#   ./scripts/check-openrouter-usage.sh          # Human-readable
#   ./scripts/check-openrouter-usage.sh --json   # JSON for scripting

set -euo pipefail

MODE="text"
CONTEXT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --json)
            MODE="json"
            shift
            ;;
        --context=*)
            CONTEXT="${1#*=}"
            shift
            ;;
        --context)
            shift
            CONTEXT="${1:-}"
            if [ -z "$CONTEXT" ]; then
                echo "--context requires a value" >&2
                exit 1
            fi
            shift
            ;;
        *)
            echo "Unknown arg: $1" >&2
            exit 1
            ;;
    esac
done

# Find the API key. Use the helper if it exists (supports --context for
# multi-account lookups); otherwise fall back to the OPENROUTER_API_KEY env var.
REPO_ROOT="$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)"
_HELPER="$REPO_ROOT/scripts/openrouter_keys.py"
if [ -f "$_HELPER" ]; then
    OPENROUTER_API_KEY=$(python3 "$_HELPER" "${CONTEXT:-}" 2>/dev/null || true)
fi
OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"

if [ -z "$OPENROUTER_API_KEY" ]; then
    echo '{"error": "OPENROUTER_API_KEY not found"}' >&2
    exit 1
fi

# Pass key and mode via env vars — never interpolate into Python string literals
OPENROUTER_API_KEY="$OPENROUTER_API_KEY" OPENROUTER_MODE="$MODE" python3 -c "
import json, os, sys, urllib.request

key = os.environ['OPENROUTER_API_KEY']
req = urllib.request.Request(
    'https://openrouter.ai/api/v1/auth/key',
    headers={'Authorization': f'Bearer {key}'}
)

try:
    resp = urllib.request.urlopen(req, timeout=10)
    raw = json.loads(resp.read())['data']
except Exception as e:
    print(json.dumps({'error': str(e)}))
    sys.exit(1)

limit = raw.get('limit')
limit_remaining = raw.get('limit_remaining')
usage_daily = raw.get('usage_daily') or 0
usage_weekly = raw.get('usage_weekly') or 0
is_unlimited = limit is None and limit_remaining is None
result = {
    'available': True if is_unlimited else (limit_remaining or 0) > 0.5,
    'utilization': 0.0 if limit is None else round(usage_daily / max(limit, 0.01), 3),
    'limit': limit,
    'limit_remaining': None if limit_remaining is None else round(limit_remaining, 2),
    'usage_daily': round(usage_daily, 2),
    'usage_weekly': round(usage_weekly, 2),
    'limit_reset': raw.get('limit_reset', 'unknown'),
    'source': 'api',
}

if os.environ.get('OPENROUTER_MODE') == 'json':
    print(json.dumps(result, indent=2))
else:
    avail = 'available' if result['available'] else 'EXHAUSTED'
    if is_unlimited:
        print(f'OpenRouter: {avail} (unlimited)')
        print(f'  Daily: \${result[\"usage_daily\"]:.2f}')
    else:
        print(f'OpenRouter: {avail}')
        print(f'  Daily: \${result[\"usage_daily\"]:.2f} / \${result[\"limit\"]} ({result[\"utilization\"]*100:.0f}%)')
        remaining = result[\"limit_remaining\"]
        if remaining is not None:
            print(f'  Remaining: \${remaining:.2f}')
        else:
            print(f'  Remaining: unknown')
    print(f'  Weekly: \${result[\"usage_weekly\"]:.2f}')
"
