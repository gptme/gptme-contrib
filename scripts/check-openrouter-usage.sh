#!/usr/bin/env bash
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

result = {
    'available': raw.get('limit_remaining', 0) > 0.5,
    'utilization': round(raw.get('usage_daily', 0) / max(raw.get('limit', 1), 0.01), 3),
    'limit': raw.get('limit'),
    'limit_remaining': round(raw.get('limit_remaining', 0), 2),
    'usage_daily': round(raw.get('usage_daily', 0), 2),
    'usage_weekly': round(raw.get('usage_weekly', 0), 2),
    'limit_reset': raw.get('limit_reset', 'unknown'),
    'source': 'api',
}

if os.environ.get('OPENROUTER_MODE') == 'json':
    print(json.dumps(result, indent=2))
else:
    avail = 'available' if result['available'] else 'EXHAUSTED'
    print(f'OpenRouter: {avail}')
    print(f'  Daily: \${result[\"usage_daily\"]:.2f} / \${result[\"limit\"]} ({result[\"utilization\"]*100:.0f}%)')
    print(f'  Remaining: \${result[\"limit_remaining\"]:.2f}')
    print(f'  Weekly: \${result[\"usage_weekly\"]:.2f}')
"
