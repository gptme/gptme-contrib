#!/usr/bin/env bash
# GraphQL attribution wrapper — logs every gh graphql call with caller metadata.
#
# Install: alias gh=/path/to/graphql-attribution.sh (or shim ~/.local/bin/gh)
# The wrapper intercepts "gh api graphql" and "gh api -X POST *graphql" patterns,
# logs them to a rolling JSONL file, then passes through to real gh.
#
# Query:  state/github-graphql-log.jsonl
# Report: ./scripts/github/graphql-attribution.sh report
#
# Output fields per log line:
#   ts, caller_pid, caller_cmd, caller_exe, query_hash, query_preview,
#   points_cost (estimated or actual), status (before/after attribution)

set -euo pipefail

# Derive log dir without subprocess: prefer explicit env var, then agent-specific
# workspace vars (absolute paths only), then script-relative.
# BOB_GRAPHQL_LOG_DIR must be set in high-call-rate environments (e.g. PM service) to
# avoid spawning `git rev-parse --show-toplevel` on every invocation.
# Note: generic WORKSPACE is intentionally excluded — it is set by GitHub Actions and
# many CI systems to the checkout root, which would silently misdirect logs.
if [ -n "${BOB_GRAPHQL_LOG_DIR:-}" ]; then
    LOG_DIR="$BOB_GRAPHQL_LOG_DIR"
elif [ -n "${BOB_WORKSPACE:-}" ] && [[ "${BOB_WORKSPACE}" = /* ]]; then
    LOG_DIR="$BOB_WORKSPACE/state"
elif [ -n "${AGENT_WORKSPACE:-}" ] && [[ "${AGENT_WORKSPACE}" = /* ]]; then
    LOG_DIR="$AGENT_WORKSPACE/state"
else
    # Pure-bash fallback: derive from script's own path without forking.
    # This relies on the script living at <repo_root>/scripts/github/graphql-attribution.sh.
    _script_dir="${BASH_SOURCE[0]%/*}"
    LOG_DIR="${_script_dir%/scripts/github}/state"
fi
LOG_FILE="$LOG_DIR/github-graphql-log.jsonl"
[ -d "$LOG_DIR" ] || mkdir -p "$LOG_DIR"

# --- Short-TTL response cache (default 300s) ---
# When GH_API_CACHE_TTL is a positive integer (seconds), read-only list/view
# calls (gh pr/issue/repo list --json, gh pr view --json) are served from a
# shared on-disk cache if an identical call ran within the TTL window. This
# collapses the identical per-session repeats that make activity-gate.sh the #1
# GraphQL burner on busy windows (tasks/graphql-cost-activity-gate-new-top-burner.md).
# Default 300s (5 min): safe for read-only calls across concurrent sessions.
# Override to 0 to disable: GH_API_CACHE_TTL=0 gh pr list ...
GH_API_CACHE_TTL="${GH_API_CACHE_TTL:-300}"
CACHE_DIR="${BOB_GRAPHQL_CACHE_DIR:-$LOG_DIR/gh-api-cache}"

# --- Resolve the real gh binary ---
_resolve_real_gh() {
    local self candidate
    self=$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")

    if [ -n "${BOB_GRAPHQL_REAL_GH_BIN:-}" ] && [ -x "$BOB_GRAPHQL_REAL_GH_BIN" ]; then
        printf '%s\n' "$BOB_GRAPHQL_REAL_GH_BIN"
        return 0
    fi

    while IFS= read -r candidate; do
        [ -n "$candidate" ] || continue
        candidate=$(readlink -f "$candidate" 2>/dev/null || printf '%s' "$candidate")
        [ "$candidate" = "$self" ] && continue
        printf '%s\n' "$candidate"
        return 0
    done < <(which -a gh 2>/dev/null || true)

    return 1
}

# Fast path: avoid the $() subshell + readlink when the env var is already set.
if [ -n "${BOB_GRAPHQL_REAL_GH_BIN:-}" ] && [ -x "$BOB_GRAPHQL_REAL_GH_BIN" ]; then
    REAL_GH_BIN="$BOB_GRAPHQL_REAL_GH_BIN"
else
    REAL_GH_BIN="$(_resolve_real_gh || true)"
    [ -n "$REAL_GH_BIN" ] || REAL_GH_BIN="gh"
fi

# --- Pre-compute effective auth identity for cache key namespacing ---
# When GH_TOKEN/GITHUB_TOKEN are set, use them directly (zero subprocesses).
# Otherwise fall back to `gh auth token` (one subprocess per invocation, not
# per call) so that stored-credential sessions get distinct cache namespaces.
# This must use REAL_GH_BIN to avoid recursive wrapper invocation.
_GH_EFFECTIVE_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
if [ -z "$_GH_EFFECTIVE_TOKEN" ]; then
    _GH_EFFECTIVE_TOKEN=$("$REAL_GH_BIN" auth token --hostname "${GH_HOST:-github.com}" 2>/dev/null || printf 'unknown')
fi

# --- Detect if this is a GraphQL-backed call ---
# Matches:
#   gh api graphql
#   gh pr list --json ...
#   gh pr view --json ...
#   gh repo list --json ...
#   gh issue list --json ...
_has_flag() {
    local needle="$1"
    shift
    local arg
    for arg in "$@"; do
        [ "$arg" = "$needle" ] && return 0
    done
    return 1
}

_is_json_list_call() {
    local args=("$@")
    local subject="${args[0]:-}"
    local verb="${args[1]:-}"
    [ "$verb" = "list" ] || return 1
    _has_flag "--json" "${args[@]}" || return 1
    case "$subject" in
        pr|repo|issue) return 0 ;;
        *) return 1 ;;
    esac
}

_is_json_pr_view_call() {
    local args=("$@")
    [ "${args[0]:-}" = "pr" ] || return 1
    [ "${args[1]:-}" = "view" ] || return 1
    _has_flag "--json" "${args[@]}"
}

_is_json_issue_view_call() {
    local args=("$@")
    [ "${args[0]:-}" = "issue" ] || return 1
    [ "${args[1]:-}" = "view" ] || return 1
    _has_flag "--json" "${args[@]}"
}

_is_graphql_call() {
    local args=("$@")
    if _is_json_list_call "${args[@]}" || _is_json_pr_view_call "${args[@]}" || _is_json_issue_view_call "${args[@]}"; then
        return 0
    fi
    for arg in "${args[@]}"; do
        [ "$arg" = "graphql" ] && return 0
    done
    return 1
}

# --- Compute a short query hash (pure bash FNV-1a, no subprocesses) ---
# Sets global _QUERY_HASH; call directly, not via $().
_QUERY_HASH=""
_query_hash() {
    local extracted="" i
    for i in "$@"; do
        if [[ "$i" == "query="* ]]; then
            extracted="${i#query=}"
            break
        fi
    done
    [ -z "$extracted" ] && extracted="$*"
    # FNV-1a 32-bit (stride-3 for speed; first 120 chars is enough for dedup)
    local s="${extracted:0:120}" h=2166136261 c
    for (( i=0; i<${#s}; i+=3 )); do
        printf -v c '%d' "'${s:$i:1}" 2>/dev/null || c=31
        (( h = (h ^ c) * 16777619 & 0xFFFFFFFF ))
    done
    printf -v _QUERY_HASH '%08x' "$h"
}

# --- Estimate cost from query type (pure bash, no subprocesses) ---
# Values calibrated against observed data: 1331 attributed calls used ~2234 actual
# points in a representative window (avg ~1.7 pts/call). Previous estimates (10-50)
# were 10-30x too high. These new values keep the ranking correct while staying
# within 2x of reality. Use calibration_ratio in graphql-window-summary.py for
# the authoritative honesty check.
# Sets global _POINTS_EST; call directly, not via $().
_POINTS_EST=1
_estimate_cost() {
    local args=("$@")
    # Fast-path: explicit gh subcommand verbs (no string building needed)
    if [ "${args[0]:-}" = "pr" ] && [ "${args[1]:-}" = "list" ]; then
        _POINTS_EST=5; return
    elif [ "${args[0]:-}" = "pr" ] && [ "${args[1]:-}" = "view" ]; then
        _POINTS_EST=3; return
    elif [ "${args[0]:-}" = "repo" ] && [ "${args[1]:-}" = "list" ]; then
        _POINTS_EST=2; return
    elif [ "${args[0]:-}" = "issue" ] && [ "${args[1]:-}" = "list" ]; then
        _POINTS_EST=2; return
    fi
    # Pattern-match on lowercased joined args (bash 4+ ${var,,} — no subprocesses)
    local j="${args[*],,}"
    if [[ "$j" == *"pullrequest"* || "$j" == *"pull_request"* || "$j" == *" prs"* ]]; then
        _POINTS_EST=5  # PR list/detail queries
    elif [[ "$j" == *"reviewthread"* || "$j" == *"review_thread"* ]]; then
        _POINTS_EST=4  # review threads per PR
    elif [[ "$j" == *"rate_limit"* || "$j" == *"ratelimit"* ]]; then
        _POINTS_EST=1  # minimal
    elif [[ "$j" == *"search"* ]]; then
        _POINTS_EST=3  # search queries
    elif [[ "$j" == *"repository"*"issues"* || "$j" == *"issues"*"first"* ]]; then
        _POINTS_EST=2  # issue list
    elif [[ "$j" == *"statuscheck"* || "$j" == *"checkrun"* || "$j" == *"checksuite"* ]]; then
        _POINTS_EST=3  # CI status
    else
        _POINTS_EST=1  # generic / simple query
    fi
}

# Sets global _QUERY_PREVIEW; call directly, not via $().
_QUERY_PREVIEW=""
_query_preview() {
    local joined="" arg
    for arg in "$@"; do joined+="$arg "; done
    if [[ "$joined" == *"query="* || "$joined" == *"query "* || "$joined" == *"mutation"* ]]; then
        for arg in "$@"; do
            if [[ "$arg" == "query="* || "$arg" == "mutation"* ]]; then
                _QUERY_PREVIEW="${arg:0:120}"
                return 0
            fi
        done
    fi
    _QUERY_PREVIEW="${joined:0:120}"
}

# --- Log a GraphQL call (pure bash, no python3 subprocess) ---
# Timestamp uses bash built-in printf %T (bash 4.2+); writes local time + literal Z.
# On UTC systems this is correct ISO-8601; on non-UTC systems the Z is still appended
# but the time is local — acceptable for internal attribution logs.
# Set BOB_GRAPHQL_SKIP_CALLER_LOOKUP=1 (e.g. in PM service) to skip readlink+tr
# per-call, eliminating the last 2 subprocesses from the hot path.
_log_graphql_call() {
    local caller_pid="$1" query_hash="$2" query_preview="$3" status="$4"
    local points_est="$5"
    local caller_exe="unknown" caller_cmd="unknown"

    if [ "${BOB_GRAPHQL_SKIP_CALLER_LOOKUP:-0}" != "1" ]; then
        caller_exe=$(readlink -f /proc/"$caller_pid"/exe 2>/dev/null || echo "unknown")
        caller_cmd=$(tr '\0' ' ' < /proc/"$caller_pid"/cmdline 2>/dev/null || echo "unknown")
        [ "${#caller_cmd}" -gt 200 ] && caller_cmd="${caller_cmd:0:200}..."
    fi

    # Timestamp via bash built-in (bash 4.2+, no subprocess)
    local ts
    printf -v ts '%(%Y-%m-%dT%H:%M:%SZ)T' -1

    # Inline JSON escaping (pure bash, no subprocesses)
    local _exe="${caller_exe//\\/\\\\}"; _exe="${_exe//\"/\\\"}"; _exe="${_exe//$'\n'/ }"; _exe="${_exe//$'\t'/ }"
    local _cmd="${caller_cmd//\\/\\\\}"; _cmd="${_cmd//\"/\\\"}"; _cmd="${_cmd//$'\n'/ }"; _cmd="${_cmd//$'\t'/ }"
    local _hash="${query_hash//\\/\\\\}"; _hash="${_hash//\"/\\\"}"
    local _prev="${query_preview//\\/\\\\}"; _prev="${_prev//\"/\\\"}"; _prev="${_prev//$'\n'/ }"; _prev="${_prev//$'\t'/ }"

    printf '{"ts":"%s","caller_pid":%s,"caller_exe":"%s","caller_cmd":"%s","query_hash":"%s","query_preview":"%s","points_est":%s,"status":"%s"}\n' \
        "$ts" "$caller_pid" "$_exe" "$_cmd" "$_hash" "$_prev" "$points_est" "$status" \
        >> "$LOG_FILE"
}

# --- Cleanup stale .tmp cache files (rate-limited to once per 5 min) ---
# .tmp.XXXXXX files are created per-call in CACHE_DIR and cleaned up on success/failure.
# On SIGKILL (e.g. OOM), cleanup never runs — files accumulate. With 100k+ entries in
# CACHE_DIR, kernel directory page-cache alone can exhaust the cgroup MemoryMax, causing
# the next PM run to OOM-kill and leave more .tmp files (death spiral, 2026-07-09).
_cleanup_stale_cache_tmp() {
    [ -d "$CACHE_DIR" ] || return 0
    local _marker="$CACHE_DIR/.cleanup-marker" _now _mtime
    printf -v _now '%(%s)T' -1
    if [ -f "$_marker" ]; then
        IFS= read -r _mtime < "$_marker" 2>/dev/null || _mtime=0
        (( _now - _mtime < 300 )) && return 0  # at most once per 5 min
    fi
    # Only delete .tmp.* files older than 2 min — safely past any in-flight write.
    find "$CACHE_DIR" -maxdepth 1 -name '.tmp.*' -mmin +2 -delete 2>/dev/null || true
    printf '%s\n' "$_now" > "$_marker"
}

# --- Rotate log if grown large (call once per script invocation, not per log call) ---
# Rate-limited to once per 60s via a marker file to avoid spawning `wc` on every call.
# On a hot PM day this function can be called 1000+/min; the marker amortizes the cost
# to one real line-count per minute instead of one subprocess per invocation.
_rotate_log_if_needed() {
    [ -f "$LOG_FILE" ] || return 0
    local _marker="$LOG_FILE.rotate-marker" _now _mtime
    printf -v _now '%(%s)T' -1  # bash 4.2+ built-in — no subprocess
    if [ -f "$_marker" ]; then
        IFS= read -r _mtime < "$_marker" 2>/dev/null || _mtime=0
        (( _now - _mtime < 60 )) && return 0
    fi
    # Time to check: count lines without wc subprocess using bash read loop.
    # Log is at most 5000 lines × ~300 bytes = 1.5 MB — acceptable to read once/min.
    local lc=0 _line
    while IFS= read -r _line; do (( ++lc )); done < "$LOG_FILE"
    if [ "$lc" -gt 5000 ]; then
        local tmp
        tmp=$(mktemp)
        tail -n 4000 "$LOG_FILE" > "$tmp" && mv -f "$tmp" "$LOG_FILE" || rm -f "$tmp"
    fi
    printf '%s\n' "$_now" > "$_marker"
}

# --- Report mode ---
_report_mode() {
    if [ ! -f "$LOG_FILE" ]; then
        echo "No GraphQL attribution log found at $LOG_FILE"
        exit 0
    fi

    echo "=== GraphQL Attribution Report ==="
    echo "Log file: $LOG_FILE"
    echo ""
    echo "Top consumers by estimated points:"
    python3 -c "
import json
from collections import Counter, defaultdict
from pathlib import Path

log_path = Path('$LOG_FILE')
if not log_path.exists():
    exit(0)

calls = []
for line in log_path.read_text().strip().split('\n'):
    if line:
        try:
            calls.append(json.loads(line))
        except json.JSONDecodeError:
            pass

# By caller cmd (truncated to executable name)
by_caller = defaultdict(lambda: {'count': 0, 'points': 0})
for c in calls:
    cmd = c.get('caller_cmd', 'unknown')
    # Extract just the script name
    import re
    m = re.search(r'/([^/\s]+\.(py|sh))', cmd)
    key = m.group(1) if m else cmd[:60]
    by_caller[key]['count'] += 1
    by_caller[key]['points'] += c.get('points_est', 0)

print(f'Total calls logged: {len(calls)}')
print(f'Total estimated points: {sum(c.get(\"points_est\", 0) for c in calls)}')
print()
print('By consumer:')
for k in sorted(by_caller, key=lambda x: by_caller[x]['points'], reverse=True):
    v = by_caller[k]
    print(f'  {k:50s} {v[\"count\"]:5d} calls  {v[\"points\"]:6d} est. points')
print()

# By query hash (show unusual patterns)
by_hash = Counter()
for c in calls:
    h = c.get('query_hash', '?')
    by_hash[h] += 1
    if c.get('status') in ('error',):
        print(f'ERROR on hash {h}: {c.get(\"query_preview\", \"?\")[:80]}')
    " 2>/dev/null || echo "  (python report failed; raw log: $LOG_FILE)"
    echo ""
    echo "Raw log: head -50 $LOG_FILE"
}

# --- Response cache helpers (only active when GH_API_CACHE_TTL > 0) ---
_cache_enabled() {
    [[ "$GH_API_CACHE_TTL" =~ ^[0-9]+$ ]] && [ "$GH_API_CACHE_TTL" -gt 0 ]
}

# Only read-only list/view calls are safe to cache. Raw `graphql` is excluded
# because it can carry mutations.
_is_cacheable_call() {
    _is_json_list_call "$@" || _is_json_pr_view_call "$@" || _is_json_issue_view_call "$@"
}

# Pure-bash FNV-1a cache key — sets _CACHE_KEY global; call directly, not via $().
# Replaces printf|md5sum|cut pipeline (3 subprocesses → 0). Hashes every character
# of the full argument string (stride-1, no length cap) to avoid collisions.
# GH_HOST and the effective token (pre-computed from GH_TOKEN/GITHUB_TOKEN or
# `gh auth token` fallback) are prepended so calls against different GitHub
# instances or authenticated identities never share cache entries. The token is
# mixed into the hash (not stored), so cache filenames reveal nothing about the
# credential. Callers needing guaranteed-fresh data should set
# GH_API_CACHE_TTL=0 to bypass the cache entirely.
_CACHE_KEY=""
_cache_key() {
    local s="${GH_HOST:-github.com}:${_GH_EFFECTIVE_TOKEN}:$*" h=2166136261 i c
    local len="${#s}"
    for (( i=0; i<len; i+=1 )); do
        printf -v c '%d' "'${s:$i:1}" 2>/dev/null || c=31
        (( h = (h ^ c) * 16777619 & 0xFFFFFFFF ))
    done
    printf -v _CACHE_KEY '%08x' "$h"
}

# Serve an identical recent call from cache, or run real gh and cache success.
# Logs a single "cache_hit" row (points 0) on a hit, else the normal
# pending/done pair. Errors are never cached.
# Serve an identical recent call from cache, or run real gh and cache success.
# Uses a .ts sidecar file for write-timestamp instead of `stat -c %Y` (eliminates
# 2 subprocesses per cache check). `printf -v now '%(%s)T' -1` replaces `date +%s`
# (eliminates 2 more). Cache key via _cache_key() global eliminates 4 more.
_run_with_cache() {
    local caller_pid="$1" query_hash="$2" query_preview="$3" points_est="$4"
    shift 4
    [ -d "$CACHE_DIR" ] || mkdir -p "$CACHE_DIR"  # conditional — saves 1 fork when dir exists
    _cache_key "$@"  # sets _CACHE_KEY — no subprocess
    local out_file ts_file
    out_file="$CACHE_DIR/$_CACHE_KEY.out"
    ts_file="$CACHE_DIR/$_CACHE_KEY.ts"

    if [ -f "$ts_file" ] && [ -f "$out_file" ]; then
        local now cached_ts age
        printf -v now '%(%s)T' -1  # bash 4.2+ built-in — no subprocess
        IFS= read -r cached_ts < "$ts_file" 2>/dev/null || cached_ts=0
        age=$(( now - cached_ts ))
        if [ "$age" -ge 0 ] && [ "$age" -lt "$GH_API_CACHE_TTL" ]; then
            _log_graphql_call "$caller_pid" "$query_hash" "$query_preview" "cache_hit" 0
            cat "$out_file"
            return 0
        fi
    fi

    _log_graphql_call "$caller_pid" "$query_hash" "$query_preview" "pending" "$points_est"
    local tmp_file gh_exit=0
    tmp_file=$(mktemp "$CACHE_DIR/.tmp.XXXXXX")
    "$REAL_GH_BIN" "$@" >"$tmp_file" 2>&1 || gh_exit=$?
    cat "$tmp_file"
    if [ "$gh_exit" -eq 0 ]; then
        mv -f "$tmp_file" "$out_file"
        printf -v now '%(%s)T' -1
        printf '%s\n' "$now" > "$ts_file"
        _log_graphql_call "$caller_pid" "$query_hash" "$query_preview" "done" "$points_est"
    else
        rm -f "$tmp_file"
        _log_graphql_call "$caller_pid" "$query_hash" "$query_preview" "failed" "$points_est"
    fi
    return "$gh_exit"
}

# --- Main ---
if [ $# -ge 1 ] && [ "$1" = "report" ]; then
    _report_mode
    exit 0
fi

# Rotate log + clean up stale .tmp files once per invocation (rate-limited internally)
_rotate_log_if_needed
_cleanup_stale_cache_tmp

# Not a GraphQL call — pass through
if ! _is_graphql_call "$@"; then
    exec "$REAL_GH_BIN" "$@"
fi

# GraphQL call — compute metadata via direct calls (no $() subshells)
CALLER_PID=$PPID
_query_hash "$@"      # sets _QUERY_HASH
_query_preview "$@"   # sets _QUERY_PREVIEW
_estimate_cost "$@"   # sets _POINTS_EST
QUERY_HASH="$_QUERY_HASH"
QUERY_PREVIEW="$_QUERY_PREVIEW"
POINTS_EST="$_POINTS_EST"

# Optional short-TTL cache for read-only list/view calls (default OFF).
if _cache_enabled && _is_cacheable_call "$@"; then
    _run_with_cache "$CALLER_PID" "$QUERY_HASH" "$QUERY_PREVIEW" "$POINTS_EST" "$@"
    exit $?
fi

_log_graphql_call "$CALLER_PID" "$QUERY_HASH" "$QUERY_PREVIEW" "pending" "$POINTS_EST"

# Execute
"$REAL_GH_BIN" "$@" 2>&1
GH_EXIT=$?

# Log result
_log_graphql_call "$CALLER_PID" "$QUERY_HASH" "$QUERY_PREVIEW" "done" "$POINTS_EST"
exit $GH_EXIT
