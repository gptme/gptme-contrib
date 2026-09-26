# shellcheck shell=bash
# log-hygiene.sh — reusable disk/log hygiene mechanisms for long-running agents.
#
# Mechanism-only by design: every function takes its target as an argument and
# reads no agent-specific paths, so an agent can source it and supply its own
# *policy* (which dirs, which retention) in the caller.
#
# On a multi-month VM, unbounded journald + accumulating gate/temp state is a
# guaranteed, boring, total outage. This is the boring code that prevents it.
#
# Safety principle: these functions only ever touch ephemeral operational state
# (gate results, per-session temp caches) and OS journals. They MUST NOT be
# pointed at trajectories, session records, or any data that captures what an
# agent did — deleting that history is unrecoverable and defeats later analysis.
# Keep deletion globs scoped to ephemeral state only.
#
# Capability tier: Tier 0 (filesystem) for hygiene_prune_old_files and
# hygiene_prune_uv_cache; the journal vacuum degrades visibly to a no-op when
# journalctl is absent (non-systemd hosts), so nothing here assumes root or a
# specific service manager.
#
# All functions honour a trailing `dry_run` arg ("true"/"false"): when true they
# report what they would do and change nothing.

# Cap the *user* systemd journal. Bounds both size and age so a months-long VM
# can't let the user journal grow without limit. Only touches the invoking user's
# journals, never the system journal.
#
# Root is NOT required *when the journal is genuinely user-owned* (Storage=auto
# with per-user journals the user can write). It is NOT usable when the host
# stores everything in a root-owned system journal under /var/log/journal and
# `journalctl --user` merely filters it by UID — there the vacuum fails with
# "Permission denied" and this function WARNs rather than pretending it capped
# anything. That case is a host/root config task (SystemMaxUse), not an agent one.
#
# Usage: hygiene_vacuum_user_journal <max_size> <max_time> [dry_run]
#   e.g. hygiene_vacuum_user_journal 200M 30d false
hygiene_vacuum_user_journal() {
    local max_size="$1" max_time="$2" dry_run="${3:-false}"

    if ! command -v journalctl >/dev/null 2>&1; then
        echo "  journalctl not present — skipping (Tier-0 degrade)"
        return 0
    fi

    local before
    before=$(journalctl --user --disk-usage 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="up") print $(i+1)}')
    echo "  User journal before: ${before:-unknown}"

    if [[ "$dry_run" == "true" ]]; then
        echo "  [dry-run] Would vacuum user journal to --vacuum-size=$max_size --vacuum-time=$max_time"
        return 0
    fi

    # vacuum-size and vacuum-time are independent caps; run both. Capture stderr
    # so a permission failure is a visible WARN, not a silent no-op: on a host where
    # `journalctl --user` maps to a root-owned system journal (Storage=persistent
    # under /var/log/journal), a non-root agent cannot vacuum it — the calls fail
    # with "Permission denied". Swallowing that would make the function *look* like
    # it capped the journal while reclaiming nothing (the capability-tier trap: a
    # Tier-1 action that silently does nothing).
    local vac_err
    vac_err=$(
        journalctl --user --vacuum-size="$max_size" 2>&1 >/dev/null
        journalctl --user --vacuum-time="$max_time" 2>&1 >/dev/null
    )

    local after
    after=$(journalctl --user --disk-usage 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="up") print $(i+1)}')
    echo "  User journal after:  ${after:-unknown}"

    if grep -qi 'permission denied\|failed to' <<<"$vac_err"; then
        echo "  WARN: journal is not user-manageable here (system-owned under" \
            "/var/log/journal). Vacuum reclaimed nothing — capping needs root" \
            "(set SystemMaxUse in journald.conf). This is a host/root task, not" \
            "an agent task."
    fi
}

# Delete files under a directory older than N days, matched by a glob. Reports
# how many were removed and how many remain. Ephemeral state only.
#
# Usage: hygiene_prune_old_files <dir> <glob> <keep_days> [dry_run]
# Echoes the number of files pruned on the last line (for callers that tally).
hygiene_prune_old_files() {
    local dir="$1" glob="$2" keep_days="$3" dry_run="${4:-false}"
    local deleted=0

    [[ -d "$dir" ]] || { echo "0"; return 0; }

    local now cutoff_secs
    now=$(date +%s)
    cutoff_secs=$((keep_days * 86400))

    local f mtime age
    while IFS= read -r -d '' f; do
        mtime=$(stat -c %Y "$f" 2>/dev/null) || continue
        age=$((now - mtime))
        if [[ "$age" -gt "$cutoff_secs" ]]; then
            if [[ "$dry_run" == "true" ]]; then
                echo "  [dry-run] Would delete: $(basename "$f")" >&2
            else
                rm "$f"
            fi
            deleted=$((deleted + 1))
        fi
    done < <(find "$dir" -maxdepth 1 -name "$glob" -print0 2>/dev/null)

    echo "$deleted"
}

# Prune unreachable entries from the uv package manager cache.
#
# Uses `uv cache prune --force` — NOT `uv cache clean` or plain `uv cache prune`.
# The distinction matters: on a long-running agent VM the cache lock
# (~/.cache/uv/.lock) is held permanently by the system service manager and
# long-lived `uv run` wrappers. `uv cache clean` and `uv cache prune` (without
# --force) block on that lock and time out after 60s. `--force` removes only
# unreachable/unused entries without acquiring the exclusive lock, so it is safe
# to run at any time with live uv consumers.
#
# Reports disk usage before and after. Degrades to a visible no-op when uv is not
# in PATH (Tier-0 safe).
#
# Usage: hygiene_prune_uv_cache [dry_run]
#   e.g. hygiene_prune_uv_cache false
hygiene_prune_uv_cache() {
    local dry_run="${1:-false}"

    if ! command -v uv >/dev/null 2>&1; then
        echo "  uv not present — skipping (Tier-0 degrade)"
        return 0
    fi

    local before=""
    # Neutralize the assignment status: a plain `before=$(...)` fails under
    # `set -e` if `uv cache dir` exits non-zero (misconfigured cache, wrapper),
    # aborting this prune AND every subsequent hygiene step in the caller.
    before=$(uv cache dir 2>/dev/null) || before=""
    local before_size="unknown"
    if [[ -n "$before" && -d "$before" ]]; then
        # Capture the value, then fall back on emptiness: `du | cut || echo` never
        # reaches the fallback (a pipeline's status is cut's, which exits 0 on
        # empty input), and `2>/dev/null` hides du's failure — leaving a blank
        # size that silently masks the error.
        before_size=$(du -sh "$before" 2>/dev/null | cut -f1)
        [[ -n "$before_size" ]] || before_size="unknown"
    fi
    echo "  uv cache before: ${before_size}"

    if [[ "$dry_run" == "true" ]]; then
        echo "  [dry-run] Would run: uv cache prune --force"
        return 0
    fi

    # Capture the exit status so a failed prune is a visible WARN, not a silent
    # no-op. Same reasoning as hygiene_vacuum_user_journal: a caller trusts this
    # to reclaim disk, so swallowing a failure would make it *look* like space
    # was freed while reclaiming nothing (the capability-tier trap).
    local prune_rc=0
    local prune_out
    prune_out=$(uv cache prune --force 2>&1) || prune_rc=$?
    echo "$prune_out" | tail -1
    if [[ $prune_rc -ne 0 ]]; then
        echo "  WARN: uv cache prune failed (exit ${prune_rc}) — cache not reclaimed"
    fi

    local after_size="unknown"
    if [[ -n "$before" && -d "$before" ]]; then
        after_size=$(du -sh "$before" 2>/dev/null | cut -f1)
        [[ -n "$after_size" ]] || after_size="unknown"
    fi
    echo "  uv cache after:  ${after_size}"
}
