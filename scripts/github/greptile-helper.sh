#!/usr/bin/env bash
# greptile-helper.sh — Safe Greptile trigger with anti-spam guards
#
# Usage:
#   greptile-helper.sh check <repo> <pr_number>   # Check status, exit 0=ok-to-trigger 1=skip
#   greptile-helper.sh trigger <repo> <pr_number> # Trigger once if safe, else skip
#   greptile-helper.sh status <repo> <pr_number>  # Print status string:
#     already-reviewed | needs-re-review | in-progress | awaiting-initial-review | stale | error
#
# Exit codes for 'check':
#   0 = safe to trigger (re-review needed: score < 5/5 + new commits)
#   1 = skip: no review yet (awaiting Greptile auto-review), or re-review trigger in-flight
#   2 = skip: reviewed by greptile-apps[bot], score=5/5 or no new commits since review
#   3 = api error (fail-safe = skip)
#
# Erik's requests (ErikBjare/bob#434):
#   1. Reduce 30min age guard → 15min (reviews complete in 5-15min)
#   2. Re-request after addressing feedback: if score < 5/5 AND new commits → trigger
#
# Initial review policy: Greptile automatically reviews all new PRs. We NEVER manually
# trigger initial reviews. Only re-reviews (score < 5/5 + new commits) are triggered.
# Status 'awaiting-initial-review' is returned for ALL unreviewed PRs regardless of age.
#
# Root cause of spam incidents:
#   Multiple concurrent sessions each check "any trigger comments?" → all see 0
#   (due to API latency or concurrent execution) → all trigger.
#   Bot ack check is more reliable: Greptile reacts within ~5-10s of trigger.

set -euo pipefail

REPO="${2:-}"
PR_NUMBER="${3:-}"
TRIGGER_GRACE_SECONDS="${TRIGGER_GRACE_SECONDS:-900}"
ACK_GRACE_SECONDS="${ACK_GRACE_SECONDS:-1200}"
MAX_RE_TRIGGERS="${MAX_RE_TRIGGERS:-3}"  # Max re-review triggers per review cycle before backing off
GITHUB_AUTHOR="${GITHUB_AUTHOR:-$(gh api user --jq .login 2>/dev/null || echo "")}"

if [ -z "$REPO" ] || [ -z "$PR_NUMBER" ]; then
    echo "Usage: $0 <check|trigger|status> <repo> <pr_number>" >&2
    exit 1
fi

if [ -z "$GITHUB_AUTHOR" ]; then
    echo "Error: GITHUB_AUTHOR not set and gh api user failed" >&2
    exit 3
fi

_json_field() {
    # Reset EXIT trap — this runs in a subshell (right side of pipe) that
    # inherits the parent's trap, which would delete the cache file.
    trap - EXIT
    local field="$1"
    python3 -c "import json, sys; data = json.load(sys.stdin); v = data.get('$field'); print(v if v is not None else '')" 2>/dev/null
}

_timestamp_gt() {
    python3 - "$1" "$2" <<'PY'
from datetime import datetime
import sys

lhs = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
rhs = datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
raise SystemExit(0 if lhs > rhs else 1)
PY
}

_age_seconds() {
    python3 - "$1" <<'PY'
from datetime import datetime, timezone
import sys

created = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
now = datetime.now(timezone.utc)
print(int((now - created).total_seconds()))
PY
}

# --- Helper: get greptile review info (score + timestamp of latest review) ---
# Returns JSON: {"has_review": bool, "score": int|null, "reviewed_at": str|null}
# IMPORTANT: Uses updated_at (not created_at) because Greptile updates its review
# comment in-place on re-reviews. Using created_at caused infinite re-trigger loops
# since commits always appeared "new" relative to the original post date.
# Cache review info via temp file to avoid redundant API calls.
# Shell variable caching doesn't work here because callers use $() subshells.
_REVIEW_CACHE_FILE="${TMPDIR:-/tmp}/greptile-review-cache-$$.json"
trap 'rm -f "$_REVIEW_CACHE_FILE"' EXIT

# Shared hash for per-PR state files (lock + trigger timestamp).
# Used across trigger and _our_trigger_status to coordinate without the GitHub API.
_PR_HASH=$(printf '%s#%s' "$REPO" "$PR_NUMBER" | (md5sum 2>/dev/null || md5 -q) | cut -c1-12)
# Local trigger timestamp file: written when a trigger is posted.
# Checked in _our_trigger_status as a fast-path BEFORE querying GitHub API.
# Guards against API propagation delay (comments posted can take minutes to appear
# in the API, causing sequential post-session pipeline calls to re-trigger).
# See: 2026-03-19 INCIDENT #5 (gptme-contrib#504/#505 got 2-3 triggers each
# because the 00:15Z trigger wasn't visible in API at 00:20Z check).
_TRIGGER_TS_FILE="${TMPDIR:-/tmp}/greptile-trigger-ts-${_PR_HASH}.txt"
_greptile_review_info() {
    # Reset EXIT trap in subshell context — callers use $() which inherits
    # the parent trap and would delete the cache file immediately on return.
    trap - EXIT
    if [ -f "$_REVIEW_CACHE_FILE" ]; then
        cat "$_REVIEW_CACHE_FILE"
        return
    fi
    # Paginate first, then apply jq filter (--paginate + --jq applies per-page,
    # producing multiple JSON objects that break downstream json.load).
    gh api "repos/$REPO/issues/$PR_NUMBER/comments" --paginate 2>/dev/null \
        | jq -s '[.[][] | select(.user.login | test("greptile"; "i"))] | sort_by(.updated_at) | last |
              if . == null then {"has_review": false, "score": null, "reviewed_at": null}
              else {
                "has_review": true,
                "reviewed_at": .updated_at,
                "score": (.body | capture("Score[*:]*\\s*(?<n>[0-9])/5") | .n | tonumber? // null)
              }
              end' > "$_REVIEW_CACHE_FILE" 2>/dev/null || echo '{"has_review": false, "score": null, "reviewed_at": null}' > "$_REVIEW_CACHE_FILE"
    cat "$_REVIEW_CACHE_FILE"
}

# --- Helper: check if greptile-apps[bot] has already reviewed ---
_has_greptile_review() {
    local info
    info=$(_greptile_review_info) || return 3
    echo "$info" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('has_review') else 1)" 2>/dev/null
}

# --- Helper: check if re-review is needed (not 5/5 + new commits since review) ---
# Returns 0 = re-review needed, 1 = no re-review needed
_needs_re_review() {
    local info score reviewed_at new_commits
    info=$(_greptile_review_info) || return 1
    score=$(echo "$info" | python3 -c "import sys,json; d=json.load(sys.stdin); s=d.get('score'); print(s) if s is not None else print('none')" 2>/dev/null) || score="none"
    reviewed_at=$(echo "$info" | _json_field "reviewed_at") || reviewed_at=""

    # Score 5 or unknown → no re-review
    if [ "$score" = "5" ] || [ "$score" = "none" ]; then
        return 1
    fi

    # Score < 5 → check for new commits since review
    if [ -z "$reviewed_at" ]; then
        return 1
    fi

    new_commits=$(gh api "repos/$REPO/pulls/$PR_NUMBER/commits" --paginate \
        2>/dev/null | jq -s "[.[][] | select(.commit.committer.date > \"$reviewed_at\")] | length" \
        2>/dev/null) || new_commits="0"

    [ "${new_commits:-0}" -gt 0 ]
}

# --- Helper: check our last trigger comment + its reactions ---
# Returns: "none" | "in-progress" | "stale"
# "in-progress" = recent trigger (< 15min), or Greptile acked it recently and hasn't reviewed yet
# "stale" = last trigger is still the latest cycle, but it's older than the grace window
_our_trigger_status() {
    local review_cutoff="${1:-}"

    # Fast-path: check local timestamp file before hitting the GitHub API.
    # The trigger command writes this file when posting a comment.  GitHub API
    # can take several minutes to surface new comments (propagation delay), so
    # sequential callers that run within TRIGGER_GRACE_SECONDS of a successful
    # trigger would otherwise see "no trigger found" and fire again.
    if [ -f "$_TRIGGER_TS_FILE" ]; then
        local _local_ts
        _local_ts=$(cat "$_TRIGGER_TS_FILE" 2>/dev/null || true)
        if [ -n "$_local_ts" ]; then
            # Only count this entry if it's from the CURRENT review cycle
            # (i.e., the timestamp is after the last Greptile review).
            # Note: when review_cutoff is empty (no prior Greptile review), skip the
            # fast-path — the trigger command only writes this file during re-reviews,
            # which always have a non-empty cutoff, so this invariant holds.
            local _ts_in_cycle=0  # 1 = TS is from current review cycle; 0 = skip fast-path
            if [ -n "$review_cutoff" ]; then
                _timestamp_gt "$_local_ts" "$review_cutoff" 2>/dev/null && _ts_in_cycle=1 || true
            fi
            if [ "$_ts_in_cycle" -eq 1 ]; then
                local _local_age
                _local_age=$(_age_seconds "$_local_ts" 2>/dev/null) || _local_age=9999
                if [ "${_local_age:-9999}" -lt "$TRIGGER_GRACE_SECONDS" ]; then
                    echo "in-progress"
                    return 0
                fi
            fi
        fi
    fi

    # Get our latest @greptileai trigger comment ID and timestamp
    # On API error: return "in-progress" (fail-safe) rather than "none" (fail-open),
    # to prevent rate-limit-caused spam. See: 2026-03-17 (root cause #1) and 2026-03-18 incidents.
    local comment_info
    # Paginate first, then filter (--paginate + --jq applies per-page).
    # Also compute count_since_review = number of our triggers after review_cutoff (for max-retries guard).
    comment_info=$(gh api "repos/$REPO/issues/$PR_NUMBER/comments" --paginate \
        2>/dev/null | jq -s '
          [.[][] | select(.user.login == "'"${GITHUB_AUTHOR}"'" and (.body | test("greptileai"; "i")))]
          | sort_by(.created_at)
          | {
              last: last,
              count_since_review: ([.[] | select(.created_at > "'"${review_cutoff}"'")] | length)
            }
          | if .last == null then {} else {id: .last.id, created_at: .last.created_at, count_since_review: .count_since_review} end
        ' \
        2>/dev/null) || { echo "in-progress"; return 0; }

    if [ -z "$comment_info" ] || [ "$comment_info" = "{}" ]; then
        echo "none"
        return 0
    fi

    local comment_id comment_age_seconds
    comment_id=$(echo "$comment_info" | _json_field "id") || comment_id=""
    local created_at
    created_at=$(echo "$comment_info" | _json_field "created_at") || created_at=""
    local count_since_review
    count_since_review=$(echo "$comment_info" | _json_field "count_since_review")

    if [ -z "$comment_id" ]; then
        echo "none"
        return 0
    fi

    # If Greptile has already reviewed after this trigger, the trigger is spent.
    if [ -n "$review_cutoff" ] && [ -n "$created_at" ]; then
        if ! _timestamp_gt "$created_at" "$review_cutoff" 2>/dev/null; then
            echo "none"
            return 0
        fi
    fi

    # Max-retries guard: if we've already triggered N times since the last Greptile review
    # without a new review landing, stop retrying. Prevents infinite loops when Greptile
    # acks (reacts with +1) but never posts a review (e.g., gptme#1651: 7 triggers, 0 reviews).
    if [ -n "$review_cutoff" ] && [ "${count_since_review:-0}" -ge "${MAX_RE_TRIGGERS:-3}" ]; then
        echo "in-progress"
        return 0
    fi

    # Check age of trigger comment
    if [ -n "$created_at" ]; then
        comment_age_seconds=$(_age_seconds "$created_at" 2>/dev/null) || comment_age_seconds=9999

        # Comment < 15 minutes old → treat as in-progress (reviews complete in 5-15min)
        if [ "${comment_age_seconds:-9999}" -lt "$TRIGGER_GRACE_SECONDS" ]; then
            echo "in-progress"
            return 0
        fi
    fi

    # Comment is older — check for any Greptile bot acknowledgement.
    # Greptile has used different reactions over time ("eyes", "+1"); match the bot, not the emoji.
    local bot_ack_count
    bot_ack_count=$(gh api -H "Accept: application/vnd.github+json" "repos/$REPO/issues/comments/$comment_id/reactions" \
        --jq '[.[] | select(.user.login == "greptile-apps[bot]")] | length' 2>/dev/null) || {
        echo "in-progress"
        return 0
    }

    if [ "${bot_ack_count:-0}" -gt 0 ] && [ "${comment_age_seconds:-9999}" -lt "$ACK_GRACE_SECONDS" ]; then
        echo "in-progress"
    else
        # No bot ack, or ack is too old without a review landing → stale, safe to retry.
        echo "stale"
    fi
}

# --- Main commands ---
case "${1:-}" in
check)
    # Check if safe to trigger
    if _has_greptile_review; then
        # Already reviewed — check if re-review is needed (score < 5/5 + new commits)
        if _needs_re_review; then
            reviewed_at=$( _greptile_review_info | _json_field "reviewed_at") || reviewed_at=""
            # Eligible for re-review — but check trigger isn't in-flight
            trigger_status=$(_our_trigger_status "$reviewed_at" || echo "in-progress")
            if [ "$trigger_status" = "in-progress" ]; then
                exit 1  # Re-review trigger in-flight
            fi
            exit 0  # Re-review needed
        fi
        exit 2  # Reviewed and 5/5 (or no new commits)
    fi
    # No review yet — Greptile auto-reviews new PRs. Never manually trigger initial review.
    exit 1
    ;;

trigger)
    # Exclusive file lock — prevents concurrent sessions from racing on the same PR.
    # Root cause of 2026-03-18 spam on gptme-agent-template#72,#73: multiple sessions
    # each called `gh api` for comments, all saw 0, all posted. flock makes check+post
    # atomic: the second session immediately fails the lock (-n = non-blocking), then sees the
    # first session's comment via the 15-min age guard and skips.
    # Use the shared _PR_HASH (computed at script start) for the lock file name
    _LOCK_FILE="${TMPDIR:-/tmp}/greptile-lock-${_PR_HASH}.lock"
    exec 9>"$_LOCK_FILE"
    # flock: use flock if available, otherwise skip locking (macOS without GNU coreutils)
    if command -v flock >/dev/null 2>&1 && ! flock -n 9; then
        echo "  [greptile] Another session is handling $REPO#$PR_NUMBER trigger. Skipping."
        exit 0
    fi
    # FD 9 held (lock) until script exits

    if _has_greptile_review; then
        if _needs_re_review; then
            reviewed_at=$( _greptile_review_info | _json_field "reviewed_at") || reviewed_at=""
            trigger_status=$(_our_trigger_status "$reviewed_at" || echo "in-progress")
            if [ "$trigger_status" = "in-progress" ]; then
                echo "  [greptile] Re-review trigger in-flight on $REPO#$PR_NUMBER. Skipping."
                exit 0
            fi
            echo "  [greptile] Re-triggering @greptileai review on $REPO#$PR_NUMBER (score < 5/5 + new commits)..."
            # Use REST API instead of `gh pr comment` (GraphQL) — REST has a
            # separate 5000/hour quota that's rarely exhausted.
            if gh api "repos/$REPO/issues/$PR_NUMBER/comments" -f body="@greptileai review" --silent 2>/dev/null; then
                # Record trigger timestamp locally — fast-path guard against GitHub API
                # propagation delay that causes sequential callers to see "no trigger"
                # and re-trigger. See: 2026-03-19 INCIDENT #5.
                date -u +%Y-%m-%dT%H:%M:%SZ > "$_TRIGGER_TS_FILE" 2>/dev/null || true
                echo "  [greptile] Re-triggered successfully."
            else
                echo "  [greptile] Trigger failed (non-fatal)."
            fi
        else
            echo "  [greptile] Already reviewed on $REPO#$PR_NUMBER (5/5 or no new commits). Skipping."
        fi
        exit 0
    fi

    # No review yet — let Greptile auto-review. Don't manually trigger initial review.
    echo "  [greptile] No review yet on $REPO#$PR_NUMBER. Awaiting Greptile auto-review."
    exit 0
    ;;

status)
    if _has_greptile_review; then
        if _needs_re_review; then
            reviewed_at=$(_greptile_review_info | _json_field "reviewed_at") || reviewed_at=""
            trigger_status=$(_our_trigger_status "$reviewed_at" || echo "in-progress")
            if [ "$trigger_status" = "in-progress" ]; then
                echo "in-progress"
            else
                echo "needs-re-review"
            fi
        else
            echo "already-reviewed"
        fi
    else
        # No review yet — check if there's a trigger in-flight (edge case: manual trigger)
        _ts=$(_our_trigger_status || echo 'error')
        if [ "$_ts" = "in-progress" ]; then
            echo "in-progress"
        else
            echo "awaiting-initial-review"
        fi
    fi
    ;;

*)
    echo "Usage: $0 <check|trigger|status> <repo> <pr_number>" >&2
    exit 1
    ;;
esac
