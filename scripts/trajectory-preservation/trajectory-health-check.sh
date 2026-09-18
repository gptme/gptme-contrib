#!/bin/bash
# Trajectory & session-data health check.
#
# Verifies that every harness's trajectories are being preserved. Run on a timer
# and/or after any change to a cleanup or backup script.
#
# Checks are PER HARNESS (see trajectory-sources.sh), because the failure mode
# this guards against is a whole harness sitting unprotected while another looks
# healthy. For each harness in the table it checks three failure modes:
#
#   1. DELETION     oldest trajectory suddenly recent  → files being pruned
#   2. NO BACKUP    backup holds fewer settled units than live → backup broke
#   3. STOPPED      newest trajectory older than STALE_DAYS, on an *active*
#                   harness → sessions running but trajectories not landing
#
# "Active" = newest trajectory within ACTIVE_WINDOW_DAYS of the newest across
# ALL harnesses. This auto-adapts on failover: the moment a dormant harness
# becomes the live one, its stalls page and the now-dormant one stops paging —
# no flag anyone has to remember to flip.
#
# Usage:
#   trajectory-health-check.sh [--warn-if-oldest-newer-than DAYS]
#
# Env: TRAJECTORY_BACKUP_ROOT, SESSION_RECORDS (default
#      $WORKSPACE/state/sessions/session-records.jsonl), ACTIVE_WINDOW_DAYS (30),
#      STALE_DAYS (3), BACKUP_GRACE_HOURS (24)
#
# Note: bare `set -eu`, NOT pipefail — the helpers use `... | sort | head -1`,
# whose upstream takes SIGPIPE when head closes early; pipefail would turn that
# routine truncation into a spurious exit-141 abort.
set -eu

WORKSPACE="${WORKSPACE:-$(cd "$(dirname "$0")" && pwd)}"
SESSION_RECORDS="${SESSION_RECORDS:-$WORKSPACE/state/sessions/session-records.jsonl}"

# shellcheck source=trajectory-sources.sh
source "$(dirname "$0")/trajectory-sources.sh"

ACTIVE_WINDOW_DAYS="${ACTIVE_WINDOW_DAYS:-30}"
STALE_DAYS="${STALE_DAYS:-3}"
BACKUP_GRACE_HOURS="${BACKUP_GRACE_HOURS:-24}"

# Deletion threshold: warn if the oldest trajectory is newer than this many days
# (a sudden jump means recent files vanished). Set below the natural age of your
# oldest trajectory so it passes normally and only fires on real deletion.
WARN_DAYS="${1:---warn-if-oldest-newer-than}"
if [[ "$WARN_DAYS" == "--warn-if-oldest-newer-than" ]]; then
    WARN_DAYS="${2:-20}"
fi

EXIT_CODE=0
NOW_SECS=$(date +%s)

echo "=== Trajectory & Session Data Health Check ==="
echo "  Date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""
echo "## Trajectories by harness"

# Global newest across harnesses, to decide which harnesses are active.
GLOBAL_NEWEST=0
for TRAJ_ENTRY in "${TRAJECTORY_SOURCES[@]}"; do
    IFS='|' read -r _ TRAJ_SRC _ TRAJ_UNIT <<< "$TRAJ_ENTRY"
    ENTRY_NEWEST=$(trajectory_newest_mtime "$TRAJ_SRC" "$TRAJ_UNIT")
    if [[ -n "$ENTRY_NEWEST" && "$ENTRY_NEWEST" -gt "$GLOBAL_NEWEST" ]]; then
        GLOBAL_NEWEST="$ENTRY_NEWEST"
    fi
done

for TRAJ_ENTRY in "${TRAJECTORY_SOURCES[@]}"; do
    IFS='|' read -r TRAJ_NAME TRAJ_SRC TRAJ_DEST TRAJ_UNIT <<< "$TRAJ_ENTRY"
    echo ""
    echo "  ### $TRAJ_NAME"

    if [[ ! -d "$TRAJ_SRC" ]]; then
        # Listed-but-absent is deliberate: the harness is covered from its first
        # session. Not a failure.
        echo "    ℹ️  Not present on this host ($TRAJ_SRC)"
        continue
    fi

    LIVE_COUNT=$(trajectory_count "$TRAJ_SRC" "$TRAJ_UNIT")
    BACKUP_COUNT=$(trajectory_count "$TRAJ_DEST" "$TRAJ_UNIT")
    LIVE_SIZE=$(du -sh "$TRAJ_SRC" 2>/dev/null | cut -f1)
    echo "    Live: $LIVE_COUNT units (${LIVE_SIZE:-?} on disk)"

    if [[ "$LIVE_COUNT" -eq 0 ]]; then
        echo "    ℹ️  Source dir exists but holds no trajectories (unused harness)"
        continue
    fi

    OLDEST_MTIME=$(trajectory_oldest_mtime "$TRAJ_SRC" "$TRAJ_UNIT")
    NEWEST_MTIME=$(trajectory_newest_mtime "$TRAJ_SRC" "$TRAJ_UNIT")
    OLDEST_AGE_DAYS=$(( (NOW_SECS - OLDEST_MTIME) / 86400 ))
    NEWEST_AGE_DAYS=$(( (NOW_SECS - NEWEST_MTIME) / 86400 ))
    echo "    Oldest: $(date -d "@$OLDEST_MTIME" +%Y-%m-%d) — ${OLDEST_AGE_DAYS}d ago"
    echo "    Newest: $(date -d "@$NEWEST_MTIME" +%Y-%m-%d) — ${NEWEST_AGE_DAYS}d ago"

    # 1. Deletion
    if [[ "$OLDEST_AGE_DAYS" -lt "$WARN_DAYS" ]]; then
        echo "    ⚠️  WARNING: oldest trajectory is only ${OLDEST_AGE_DAYS}d old (expected >${WARN_DAYS}d)"
        echo "       This suggests trajectories are being deleted. Check your cleanup script!"
        EXIT_CODE=1
    else
        echo "    ✅ Oldest trajectory is ${OLDEST_AGE_DAYS}d old (no sign of deletion)"
    fi

    # 2. Backup coverage. Backup >= live is healthy: the backup keeps units the
    # live dir no longer has, which is the whole point. Only units older than
    # BACKUP_GRACE_HOURS are *expected* in the backup — newer ones may post-date
    # the last backup run. If the backup stops entirely the shortfall grows past
    # the grace window within a day and pages anyway.
    EXPECTED_COUNT=$(trajectory_count_older_than "$TRAJ_SRC" "$TRAJ_UNIT" "$(( NOW_SECS - BACKUP_GRACE_HOURS * 3600 ))")
    if [[ ! -d "$TRAJ_DEST" ]]; then
        echo "    ⚠️  WARNING: no backup dir at $TRAJ_DEST — this harness is UNPROTECTED"
        EXIT_CODE=1
    elif [[ "$BACKUP_COUNT" -lt "$EXPECTED_COUNT" ]]; then
        echo "    ⚠️  WARNING: backup holds $BACKUP_COUNT of $EXPECTED_COUNT settled units — $(( EXPECTED_COUNT - BACKUP_COUNT )) unprotected"
        echo "       Run trajectory-backup.sh to refresh the hardlink backup."
        EXIT_CODE=1
    else
        echo "    ✅ Backup: $BACKUP_COUNT units (>= $EXPECTED_COUNT settled of $LIVE_COUNT live)"
    fi

    # 3. Stopped growing — only meaningful for the harness(es) currently in use.
    ACTIVE_LAG_DAYS=$(( (GLOBAL_NEWEST - NEWEST_MTIME) / 86400 ))
    if [[ "$ACTIVE_LAG_DAYS" -le "$ACTIVE_WINDOW_DAYS" ]]; then
        if [[ "$NEWEST_AGE_DAYS" -gt "$STALE_DAYS" ]]; then
            echo "    ⚠️  WARNING: active harness has produced no trajectory in ${NEWEST_AGE_DAYS}d (>${STALE_DAYS}d)"
            echo "       Trajectory writing may be broken for this harness."
            EXIT_CODE=1
        else
            echo "    ✅ Growing (newest ${NEWEST_AGE_DAYS}d old)"
        fi
    else
        echo "    ℹ️  Dormant (${ACTIVE_LAG_DAYS}d behind the active harness) — staleness check skipped"
    fi
done

echo ""

# --- Session records (optional; gptme-sessions store) ---
echo "## Session Records"
if [[ -f "$SESSION_RECORDS" ]]; then
    SR_LINES=$(wc -l < "$SESSION_RECORDS")
    SR_SIZE=$(du -h "$SESSION_RECORDS" | cut -f1)
    echo "  Records: $SR_LINES ($SR_SIZE) at $SESSION_RECORDS"
    python3 -c "
import collections, json, sys
counts = collections.Counter()
with open('$SESSION_RECORDS') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            counts[rec.get('harness') or rec.get('backend') or 'unknown'] += 1
        except (ValueError, TypeError):
            counts['unparseable'] += 1
for name, n in counts.most_common():
    print(f'  By harness: {name}: {n}')
" 2>/dev/null || echo "  By harness: (could not parse)"
else
    echo "  ℹ️  No session records file at $SESSION_RECORDS (set SESSION_RECORDS to point at one)"
fi

echo ""
echo "## Total Preserved Data"
echo "  Backup root ($TRAJECTORY_BACKUP_ROOT): $(du -sh "$TRAJECTORY_BACKUP_ROOT" 2>/dev/null | cut -f1 || echo 'unknown')"

echo ""
echo "=== Health check complete (exit code: $EXIT_CODE) ==="
exit $EXIT_CODE
