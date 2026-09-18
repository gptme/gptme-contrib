#!/bin/bash
# Trajectory hardlink backup — defense-in-depth against trajectory loss.
#
# Creates hardlinked copies of every harness's trajectories at a separate path
# (TRAJECTORY_BACKUP_ROOT), so that even if the live directory is ever pruned
# (by a bug, a model, or a future cleanup refactor) the data survives. Hardlinks
# cost zero extra disk while the source still exists, and keep the data alive
# once it does not.
#
# WHY THIS EXISTS: on 2026-04-09 a cleanup script deleted 3,011 Claude Code
# trajectories despite the harness's own retention being set to never-delete —
# it bypassed that setting by removing files externally. Nothing before that
# survives. A hardlink backup on a separate path would have made the deletion
# recoverable. This is layer 3 of defense-in-depth; it does NOT protect against
# loss of the whole filesystem (see the same-fs warning below) — for that you
# need an off-host copy (PBS, rsync to another machine).
#
# The set of harnesses to back up is DATA, not code: see trajectory-sources.sh.
#
# Usage:
#   trajectory-backup.sh [--dry-run]
#
# Env: TRAJECTORY_BACKUP_ROOT (default ~/data/trajectories),
#      TRAJECTORY_EXTRA_SOURCES, WORKSPACE (see trajectory-sources.sh)
#
# Bare `set -eu`, NOT pipefail: the sourced helpers use `... | sort | head -1`,
# whose upstream takes SIGPIPE when head closes early — pipefail would abort.
set -eu

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

# shellcheck source=trajectory-sources.sh
source "$(dirname "$0")/trajectory-sources.sh"

echo "=== Trajectory backup (rsync --link-dest, all harnesses) ==="
echo "  Backup root: $TRAJECTORY_BACKUP_ROOT"
echo ""

for TRAJ_ENTRY in "${TRAJECTORY_SOURCES[@]}"; do
    IFS='|' read -r TRAJ_NAME TRAJ_SRC TRAJ_DEST TRAJ_UNIT <<< "$TRAJ_ENTRY"

    if [[ ! -d "$TRAJ_SRC" ]]; then
        echo "  [$TRAJ_NAME] source not present ($TRAJ_SRC), skipping"
        continue
    fi

    mkdir -p "$TRAJ_DEST"
    if $DRY_RUN; then
        # `|| true`: grep exits 1 if the stats wording is absent (older rsync,
        # empty source), which would abort under `set -e`.
        RSYNC_STATS=$(rsync -a --link-dest="$TRAJ_SRC" --dry-run --stats "$TRAJ_SRC/" "$TRAJ_DEST/" 2>/dev/null \
            | grep "Number of regular files transferred" || true)
        echo "  [$TRAJ_NAME] [dry-run] $TRAJ_SRC → $TRAJ_DEST   $RSYNC_STATS"
    else
        # rsync -a preserves symlinks AS symlinks (no -L): gptme conversation
        # dirs contain a `workspace` symlink to the whole workspace that must
        # NOT be dereferenced and copied.
        rsync -a --link-dest="$TRAJ_SRC" "$TRAJ_SRC/" "$TRAJ_DEST/" 2>/dev/null
    fi
    echo "  [$TRAJ_NAME] live: $(trajectory_count "$TRAJ_SRC" "$TRAJ_UNIT") units, $(du -sh "$TRAJ_SRC" 2>/dev/null | cut -f1)" \
         "→ backup: $(trajectory_count "$TRAJ_DEST" "$TRAJ_UNIT") units, $(du -sh "$TRAJ_DEST" 2>/dev/null | cut -f1)"
done

echo ""
# Hardlinks only defend against unlink, not against loss of the filesystem.
if command -v df >/dev/null 2>&1; then
    BACKUP_FS=$(df -P "$TRAJECTORY_BACKUP_ROOT" 2>/dev/null | awk 'NR==2 {print $1}')
    SRC_FS=$(df -P "$HOME/.claude/projects" 2>/dev/null | awk 'NR==2 {print $1}')
    if [[ -n "$BACKUP_FS" && "$BACKUP_FS" == "$SRC_FS" ]]; then
        echo "  ℹ️  Backup is on the same filesystem as the source ($BACKUP_FS)."
        echo "     Hardlinks defend against deletion, not disk loss — add an off-host copy."
    fi
fi
echo "=== Backup complete ($(du -sh "$TRAJECTORY_BACKUP_ROOT" 2>/dev/null | cut -f1 || echo '?') across ${#TRAJECTORY_SOURCES[@]} sources) ==="
