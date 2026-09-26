#!/usr/bin/env bash
# auth-stale-preflight.sh — Pre-flight check for stale Claude Code auth.
#
# Exit codes:
#   0 — auth is fresh, proceed
#   75 — auth stale (skip/retry)
#
# Called from dispatchers before launching a Claude Code session. A recent
# claude-code-auth-stale.txt blocks immediately. Once its TTL expires, verify
# live auth before clearing it: TTL is a probe cadence, not evidence that a
# persistent outage recovered.
#
# Absence of a marker is also not proof of live auth. A logout that never
# writes a marker (slot=unknown during a leftover raw credentials.json, or a
# prose "OAuth session expired" death the trajectory JSON-envelope classifier
# misses) used to fall through and burn ~80s per doomed CC launch. For
# claude-code, probe `claude auth status --json` even with no marker. Fail
# open only when that probe is unavailable (empty output), so environments
# without `claude` on PATH keep historical no-marker proceed behavior.
#
# Config (all overridable via env):
#   STALE_DIR              marker directory (default: $WORKSPACE/state/backend-quota)
#   MAX_AGE_SEC            marker TTL / re-probe cadence in seconds (default 1800)
#   AUTH_PROBE_TIMEOUT_SEC live-probe timeout in seconds (default 10)
#
# Usage: auth-stale-preflight.sh [backend]

set -euo pipefail

BACKEND="${1:-}"
STALE_DIR="${STALE_DIR:-${WORKSPACE:-${REPO_ROOT:-$HOME}}/state/backend-quota}"
MAX_AGE_SEC="${MAX_AGE_SEC:-1800}"   # 30 minutes
AUTH_PROBE_TIMEOUT_SEC="${AUTH_PROBE_TIMEOUT_SEC:-10}"

if [ -z "$BACKEND" ]; then
    echo "usage: $0 <backend>  # e.g. claude-code" >&2
    exit 1
fi

# Live Claude Code auth probe.
# Returns: 0 logged in, 1 logged out / not loggedIn true, 2 probe unavailable.
_cc_live_auth_probe() {
    local status
    status=$(timeout "$AUTH_PROBE_TIMEOUT_SEC" env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT \
        -u CC_SESSION_ID -u CC_MODEL claude auth status --json 2>/dev/null || true)
    if [ -z "$status" ]; then
        return 2
    fi
    if printf '%s' "$status" | grep -Eq '"loggedIn"[[:space:]]*:[[:space:]]*true'; then
        return 0
    fi
    return 1
}

# Resolve slot-scoped marker for claude-code; fall back to legacy unscoped file.
# Writers now emit claude-code-{sub}-auth-stale.txt (skip when active_sub=unknown);
# the unscoped name is a backward-compat fallback for old writers and transitions.
if [ "$BACKEND" = "claude-code" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    # shellcheck source=lib/resolve-active-slot.sh
    source "$SCRIPT_DIR/lib/resolve-active-slot.sh"
    _ACTIVE_SUB="$(resolve_active_slot)"
    if [ "$_ACTIVE_SUB" != "unknown" ] && \
       [ -f "$STALE_DIR/claude-code-${_ACTIVE_SUB}-auth-stale.txt" ]; then
        AUTH_STALE_FILE="$STALE_DIR/claude-code-${_ACTIVE_SUB}-auth-stale.txt"
        AUTH_STALE_ERROR="$STALE_DIR/claude-code-${_ACTIVE_SUB}-auth-stale-error.txt"
    else
        AUTH_STALE_FILE="$STALE_DIR/${BACKEND}-auth-stale.txt"
        AUTH_STALE_ERROR="$STALE_DIR/${BACKEND}-auth-stale-error.txt"
    fi
    unset _ACTIVE_SUB SCRIPT_DIR
else
    AUTH_STALE_FILE="$STALE_DIR/${BACKEND}-auth-stale.txt"
    AUTH_STALE_ERROR="$STALE_DIR/${BACKEND}-auth-stale-error.txt"
fi

if [ ! -f "$AUTH_STALE_FILE" ]; then
    if [ "$BACKEND" = "claude-code" ]; then
        probe_rc=0
        _cc_live_auth_probe || probe_rc=$?
        case "$probe_rc" in
            0)
                exit 0
                ;;
            2)
                # Probe unavailable — do not invent a block.
                exit 0
                ;;
            *)
                echo "AUTH STALE BLOCK: ${BACKEND} has no stale marker, but live auth probe reports logged out/unavailable" >&2
                echo "  Suggested fix: re-authenticate claude-code (\`claude auth login\`); do not adopt a raw ~/.claude/.credentials.json until login succeeds." >&2
                exit 75
                ;;
        esac
    fi
    exit 0
fi

# Check if the stale marker is recent
if [ -f "$AUTH_STALE_FILE" ]; then
    FILE_AGE_SEC=$(( $(date +%s) - $(stat -c '%Y' "$AUTH_STALE_FILE") ))
    if [ "$FILE_AGE_SEC" -lt "$MAX_AGE_SEC" ]; then
        echo "AUTH STALE BLOCK: ${BACKEND} auth-stale file is ${FILE_AGE_SEC}s old (max ${MAX_AGE_SEC}s)" >&2
        echo "  Marker: $AUTH_STALE_FILE" >&2
        if [ -f "$AUTH_STALE_ERROR" ]; then
            echo "  Error excerpt:" >&2
            head -c 300 "$AUTH_STALE_ERROR" >&2
            echo >&2
        fi
        echo "  Suggested fix: re-authenticate claude-code or wait for the credential to refresh." >&2
        exit 75
    elif [ "$BACKEND" = "claude-code" ]; then
        # Marker TTL is only a re-probe cadence. Blindly clearing it after 30m
        # turns a persistent logout into one failed session every 30m forever.
        probe_rc=0
        _cc_live_auth_probe || probe_rc=$?
        if [ "$probe_rc" -eq 0 ]; then
            echo "AUTH STALE: ${BACKEND} marker is ${FILE_AGE_SEC}s old and live auth probe passed — clearing" >&2
            rm -f "$AUTH_STALE_FILE" "$AUTH_STALE_ERROR"
            exit 0
        fi
        # Refresh mtime so concurrent dispatchers keep taking the cheap block
        # path until the next bounded live probe. Empty-probe (rc=2) stays
        # fail-closed here because the marker is already evidence of an outage.
        touch "$AUTH_STALE_FILE"
        echo "AUTH STALE BLOCK: ${BACKEND} marker expired, but live auth probe still reports logged out/unavailable" >&2
        echo "  Suggested fix: re-authenticate claude-code; marker retained for retry." >&2
        exit 75
    else
        # Other backends have no live auth probe contract. Preserve historical
        # TTL behavior rather than blocking them indefinitely.
        echo "AUTH STALE: ${BACKEND} auth-stale marker is ${FILE_AGE_SEC}s old — stale, clearing" >&2
        rm -f "$AUTH_STALE_FILE" "$AUTH_STALE_ERROR"
        exit 0
    fi
fi

exit 0
