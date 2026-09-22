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
# Capability tier: Tier 0 (filesystem) for hygiene_prune_old_files; the journal
# vacuum degrades visibly to a no-op when journalctl is absent (non-systemd
# hosts), so nothing here assumes root or a specific service manager.
#
# All functions honour a trailing `dry_run` arg ("true"/"false"): when true they
# report what they would do and change nothing.

# Cap the *user* systemd journal (no root required — only touches the invoking
# user's journals, never the system journal). Bounds both size and age so a
# months-long VM can't let the user journal grow without limit.
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

    # vacuum-size and vacuum-time are independent caps; run both.
    journalctl --user --vacuum-size="$max_size" >/dev/null 2>&1 || true
    journalctl --user --vacuum-time="$max_time" >/dev/null 2>&1 || true

    local after
    after=$(journalctl --user --disk-usage 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="up") print $(i+1)}')
    echo "  User journal after:  ${after:-unknown}"
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
