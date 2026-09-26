#!/usr/bin/env bash
# shellcheck disable=SC2317  # sourced library; functions run in caller contexts
# auth_resilience.sh — shared 401-auth resilience primitives.
#
# Reactive bash launchers (autonomous-run, operator-loop, spawn-workers,
# dispatchers, project-monitoring) each tend to grow their own copy of:
# preflight → run → on-401 write the shared stale-marker → backoff → retry-once.
# This file is that pattern, extracted once.
#
# Source this file, then call:
#   auth_resilience_preflight [backend]
#   auth_resilience_write_marker --source NAME [--backend BACKEND] [--dir DIR] [--kv KEY=VAL ...]
#   auth_resilience_classify_file PATH
#   auth_resilience_classify_stdin
#   auth_resilience_classify_trajectory PATH
#   with_auth_resilience [options] -- command...
#
# Marker writes are slot-scoped (`claude-code-{slot}-auth-stale.txt`) and are
# skipped when the active slot is "unknown" (the /login window — see
# resolve-active-slot.sh). Never write the unscoped fleet-global filename.
#
# Sibling scripts (auth_401.py, auth-stale-preflight.sh, resolve-active-slot.sh)
# are resolved relative to THIS file's location (the shared-library layout), not
# via WORKSPACE — they ship together as one unit. State/marker directories, by
# contrast, are resolved workspace-relative so each consumer keeps its own state.
#
# Do NOT `set -euo pipefail` here. Callers have mixed option sets
# (some are `set -u` only); a sourced `set -e` would abort them.

_AUTH_RESILIENCE_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_AUTH_RESILIENCE_SCRIPTS_DIR="$(cd "$_AUTH_RESILIENCE_LIB_DIR/.." && pwd)"
_AUTH_RESILIENCE_ROOT="$(cd "$_AUTH_RESILIENCE_LIB_DIR/../.." && pwd)"

# Workspace root, used only for resolving the state/marker directory (not the
# sibling scripts). A consumer sets WORKSPACE/REPO_ROOT to its own workspace.
_auth_resilience_root() {
    echo "${WORKSPACE:-${REPO_ROOT:-$_AUTH_RESILIENCE_ROOT}}"
}

_auth_resilience_stale_dir() {
    local override="${1:-}"
    if [ -n "$override" ]; then
        echo "$override"
        return 0
    fi
    if [ -n "${STALE_DIR:-}" ]; then
        echo "$STALE_DIR"
        return 0
    fi
    if [ -n "${CRASH_STATE_DIR:-}" ]; then
        echo "$CRASH_STATE_DIR"
        return 0
    fi
    echo "${STATE_WORKSPACE:-$(_auth_resilience_root)}/state/backend-quota"
}

# Sibling scripts resolve relative to this library's own directory so a consumer
# that sources it from an unrelated WORKSPACE still finds them.
_auth_resilience_auth_401_py() {
    echo "$_AUTH_RESILIENCE_SCRIPTS_DIR/auth_401.py"
}

_auth_resilience_preflight_sh() {
    echo "$_AUTH_RESILIENCE_SCRIPTS_DIR/auth-stale-preflight.sh"
}

_auth_resilience_ensure_slot_helper() {
    if command -v resolve_active_slot >/dev/null 2>&1; then
        return 0
    fi
    # shellcheck source=resolve-active-slot.sh
    # shellcheck disable=SC1091
    source "$_AUTH_RESILIENCE_LIB_DIR/resolve-active-slot.sh"
}

# Exit 0 if auth is fresh, 75 if a recent stale-marker says skip.
auth_resilience_preflight() {
    local backend="${1:-claude-code}"
    bash "$(_auth_resilience_preflight_sh)" "$backend"
}

# Exit 0 iff PATH is a size-gated auth-death (tiny output + 401 signature).
auth_resilience_classify_file() {
    python3 "$(_auth_resilience_auth_401_py)" --classify-file "$1"
}

# Exit 0 iff stdin carries a 401/auth-failure signature (no size gate).
auth_resilience_classify_stdin() {
    python3 "$(_auth_resilience_auth_401_py)" --classify-stdin
}

# Exit 0 iff PATH is a prose-safe trajectory JSON-envelope auth-death.
auth_resilience_classify_trajectory() {
    python3 "$(_auth_resilience_auth_401_py)" --classify-trajectory "$1"
}

# Write a slot-scoped auth-stale marker so sibling launchers back off.
#
# Returns:
#   0 — marker written; path printed to stdout
#   2 — skipped (active_sub=unknown, /login in flight); nothing written
#   1 — write failed
auth_resilience_write_marker() {
    local source="unknown"
    local backend="claude-code"
    local dir=""
    local -a extra_kv=()

    while [ $# -gt 0 ]; do
        case "$1" in
            --source)
                source="${2:-}"
                shift 2
                ;;
            --backend)
                backend="${2:-claude-code}"
                shift 2
                ;;
            --dir)
                dir="${2:-}"
                shift 2
                ;;
            --kv)
                extra_kv+=("${2:-}")
                shift 2
                ;;
            *)
                echo "auth_resilience_write_marker: unknown arg $1" >&2
                return 1
                ;;
        esac
    done

    _auth_resilience_ensure_slot_helper
    local slot
    slot="$(resolve_active_slot)"

    local stale_dir
    stale_dir="$(_auth_resilience_stale_dir "$dir")"
    mkdir -p "$stale_dir" 2>/dev/null || true

    if [ "$backend" = "claude-code" ] && [ "$slot" = "unknown" ]; then
        echo "auth_resilience: skip marker (active_sub=unknown — /login in flight)" >&2
        return 2
    fi

    local marker
    if [ "$backend" = "claude-code" ]; then
        marker="${stale_dir}/claude-code-${slot}-auth-stale.txt"
    else
        marker="${stale_dir}/${backend}-auth-stale.txt"
    fi

    local tmp body kv
    body="$(date --iso-8601=seconds)
active_sub=${slot}
source=${source}"
    for kv in "${extra_kv[@]+"${extra_kv[@]}"}"; do
        body+=$'\n'"$kv"
    done

    tmp=$(mktemp "${stale_dir}/.${backend}-auth-stale.XXXXXX") || return 1
    if printf '%s\n' "$body" >"$tmp" && mv -f "$tmp" "$marker"; then
        echo "$marker"
        return 0
    fi
    rm -f "$tmp"
    return 1
}

# with_auth_resilience [options] -- command...
#
# Options:
#   --backend NAME              default claude-code
#   --retries N                 retry-once on classified 401 (default 1)
#   --backoff SEC               sleep between retries (default 5; honours SLEEP_MULT)
#   --source NAME               written into the stale marker
#   --classify-file PATH        size-gated classifier (worker/child output)
#   --classify-trajectory PATH  prose-safe trajectory classifier
#   --kv KEY=VAL                extra marker field (repeatable)
#   --on-stale skip|retry       preflight-stale behaviour (default skip)
#   --skip-preflight            caller already preflighted this cycle
#
# Exit 0 if the command succeeded (possibly after a 401 retry).
# Exit 75 if preflight blocked (and --on-stale skip, or retry still stale).
# Otherwise the command's exit code.
with_auth_resilience() {
    local backend="claude-code"
    local retries=1
    local backoff="${AUTH_RESILIENCE_BACKOFF:-5}"
    local source="with_auth_resilience"
    local classify_file=""
    local classify_trajectory=""
    local on_stale="skip"
    local skip_preflight=0
    local -a extra_kv=()

    while [ $# -gt 0 ]; do
        case "$1" in
            --backend)
                backend="${2:-claude-code}"
                shift 2
                ;;
            --retries)
                retries="${2:-1}"
                shift 2
                ;;
            --backoff)
                backoff="${2:-5}"
                shift 2
                ;;
            --source)
                source="${2:-with_auth_resilience}"
                shift 2
                ;;
            --classify-file)
                classify_file="${2:-}"
                shift 2
                ;;
            --classify-trajectory)
                classify_trajectory="${2:-}"
                shift 2
                ;;
            --kv)
                extra_kv+=("${2:-}")
                shift 2
                ;;
            --on-stale)
                on_stale="${2:-skip}"
                shift 2
                ;;
            --skip-preflight)
                skip_preflight=1
                shift
                ;;
            --)
                shift
                break
                ;;
            *)
                echo "with_auth_resilience: unknown arg $1 (expected -- command)" >&2
                return 1
                ;;
        esac
    done

    if [ $# -eq 0 ]; then
        echo "with_auth_resilience: missing command after --" >&2
        return 1
    fi

    local sleep_mult="${SLEEP_MULT:-1}"
    if ! [[ "$sleep_mult" =~ ^[0-9]+$ ]]; then
        sleep_mult=1
    fi
    if ! [[ "$backoff" =~ ^[0-9]+$ ]]; then
        backoff=5
    fi
    if ! [[ "$retries" =~ ^[0-9]+$ ]]; then
        retries=1
    fi

    if [ "$skip_preflight" -eq 0 ]; then
        local pf_exit=0
        auth_resilience_preflight "$backend" || pf_exit=$?
        if [ "$pf_exit" -ne 0 ]; then
            if [ "$on_stale" = "retry" ] && [ "$pf_exit" -eq 75 ]; then
                sleep $((backoff * sleep_mult))
                pf_exit=0
                auth_resilience_preflight "$backend" || pf_exit=$?
                if [ "$pf_exit" -ne 0 ]; then
                    return "$pf_exit"
                fi
            else
                return "$pf_exit"
            fi
        fi
    fi

    local attempt=0
    local cmd_exit=0
    while true; do
        cmd_exit=0
        "$@" || cmd_exit=$?
        if [ "$cmd_exit" -eq 0 ]; then
            return 0
        fi

        local is_401=0
        if [ -n "$classify_file" ] && [ -f "$classify_file" ]; then
            if auth_resilience_classify_file "$classify_file" >/dev/null 2>&1; then
                is_401=1
            fi
        elif [ -n "$classify_trajectory" ] && [ -f "$classify_trajectory" ]; then
            if auth_resilience_classify_trajectory "$classify_trajectory" >/dev/null 2>&1; then
                is_401=1
            fi
        fi

        if [ "$is_401" -eq 1 ]; then
            local -a marker_args=(--source "$source" --backend "$backend"
                --kv "attempt=${attempt}" --kv "cmd_exit=${cmd_exit}")
            local kv
            for kv in "${extra_kv[@]+"${extra_kv[@]}"}"; do
                marker_args+=(--kv "$kv")
            done
            auth_resilience_write_marker "${marker_args[@]}" >/dev/null || true
            if [ "$attempt" -lt "$retries" ]; then
                attempt=$((attempt + 1))
                sleep $((backoff * sleep_mult))
                continue
            fi
        fi
        return "$cmd_exit"
    done
}
