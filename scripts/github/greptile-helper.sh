#!/usr/bin/env bash
# greptile-helper.sh — Safe Greptile trigger with anti-spam guards
#
# Usage:
#   greptile-helper.sh check <repo> <pr_number>   # Check status, exit 0=ok-to-trigger 1=skip
#   greptile-helper.sh trigger <repo> <pr_number> # Trigger once if safe, else skip
#   greptile-helper.sh status <repo> <pr_number>  # Print human-readable status
#
# Exit codes for 'check':
#   0 = safe to trigger (no review yet, or re-review needed: not 5/5 + new commits)
#   1 = skip: trigger in-flight (our comment has Greptile bot ack, or comment < 15min old)
#   2 = skip: reviewed by greptile-apps[bot], score=5/5 or no new commits since review
#   3 = api error (fail-safe = skip)
#
# Erik's requests (ErikBjare/bob#434):
#   1. Reduce 30min age guard → 15min (reviews complete in 5-15min)
#   2. Re-request after addressing feedback: if score < 5/5 AND new commits → trigger
#
# Root cause of spam incidents:
#   Multiple concurrent sessions each check "any trigger comments?" → all see 0
#   (due to API latency or concurrent execution) → all trigger.
#   Bot ack check is more reliable: Greptile reacts within ~5-10s of trigger.

set -euo pipefail

REPO="${2:-}"
PR_NUMBER="${3:-}"
TRIGGER_GRACE_SECONDS=900
ACK_GRACE_SECONDS=1200

if [ -z "$REPO" ] || [ -z "$PR_NUMBER" ]; then
    echo "Usage: $0 <check|trigger|status> <repo> <pr_number>" >&2
    exit 1
fi

_json_field() {
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
_greptile_review_info() {
    gh api "repos/$REPO/issues/$PR_NUMBER/comments" \
        --jq '[.[] | select(.user.login | test("greptile"; "i"))] | sort_by(.updated_at) | last |
              if . == null then {"has_review": false, "score": null, "reviewed_at": null}
              else {
                "has_review": true,
                "reviewed_at": .updated_at,
                "score": (.body | capture("Score: (?<n>[0-9])/5") | .n | tonumber? // null)
              }
              end' 2>/dev/null || echo '{"has_review": false, "score": null, "reviewed_at": null}'
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

    new_commits=$(gh api "repos/$REPO/pulls/$PR_NUMBER/commits" \
        --jq "[.[] | select(.commit.author.date > \"$reviewed_at\")] | length" 2>/dev/null) || new_commits="0"

    [ "${new_commits:-0}" -gt 0 ]
}

# --- Helper: check our last trigger comment + its reactions ---
# Returns: "none" | "in-progress" | "stale"
# "in-progress" = recent trigger (< 15min), or Greptile acked it recently and hasn't reviewed yet
# "stale" = last trigger is still the latest cycle, but it's older than the grace window
_our_trigger_status() {
    local review_cutoff="${1:-}"
    # Get our latest @greptileai trigger comment ID and timestamp
    # On API error: return "in-progress" (fail-safe) rather than "none" (fail-open),
    # to prevent rate-limit-caused spam. See: 2026-03-17 (root cause #1) and 2026-03-18 incidents.
    local comment_info
    comment_info=$(gh api "repos/$REPO/issues/$PR_NUMBER/comments" \
        --jq '[.[] | select(.user.login == "'"${GITHUB_AUTHOR:-TimeToBuildBob}"'" and (.body | test("greptileai"; "i")))] | sort_by(.created_at) | last | if . == null then {} else {id: .id, created_at: .created_at} end' \
        2>/dev/null) || { echo "in-progress"; return 0; }

    if [ -z "$comment_info" ] || [ "$comment_info" = "{}" ]; then
        echo "none"
        return 0
    fi

    local comment_id comment_age_seconds
    comment_id=$(echo "$comment_info" | _json_field "id") || comment_id=""
    local created_at
    created_at=$(echo "$comment_info" | _json_field "created_at") || created_at=""

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

    if [ "${bot_ack_count:-0}" -gt 0 ] 2>/dev/null && [ "${comment_age_seconds:-9999}" -lt "$ACK_GRACE_SECONDS" ]; then
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

    trigger_status=$(_our_trigger_status || echo "none")
    case "$trigger_status" in
    "none" | "stale")
        exit 0  # Safe to trigger
        ;;
    "in-progress")
        exit 1
        ;;
    esac
    ;;

trigger)
    # Exclusive file lock — prevents concurrent sessions from racing on the same PR.
    # Root cause of 2026-03-18 spam on gptme-agent-template#72,#73: multiple sessions
    # each called `gh api` for comments, all saw 0, all posted. flock makes check+post
    # atomic: the second session waits for or immediately loses the lock, then sees the
    # first session's comment via the 15-min age guard and skips.
    _LOCK_FILE="${TMPDIR:-/tmp}/greptile-lock-$(printf '%s-%s' "$REPO" "$PR_NUMBER" | tr '/' '-').lock"
    exec 9>"$_LOCK_FILE"
    if ! flock -n 9; then
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
            gh pr comment "$PR_NUMBER" --repo "$REPO" --body "@greptileai review" 2>/dev/null \
                && echo "  [greptile] Re-triggered successfully." \
                || echo "  [greptile] Trigger failed (non-fatal)."
        else
            echo "  [greptile] Already reviewed on $REPO#$PR_NUMBER (5/5 or no new commits). Skipping."
        fi
        exit 0
    fi

    trigger_status=$(_our_trigger_status || echo "none")
    case "$trigger_status" in
    "in-progress")
        echo "  [greptile] Trigger in-flight on $REPO#$PR_NUMBER (recent or bot-acked). Skipping."
        exit 0
        ;;
    "none" | "stale")
        echo "  [greptile] Triggering @greptileai review on $REPO#$PR_NUMBER..."
        gh pr comment "$PR_NUMBER" --repo "$REPO" --body "@greptileai review" 2>/dev/null \
            && echo "  [greptile] Triggered successfully." \
            || echo "  [greptile] Trigger failed (non-fatal)."
        ;;
    esac
    ;;

status)
    if _has_greptile_review; then
        if _needs_re_review; then
            echo "needs-re-review"
        else
            echo "already-reviewed"
        fi
    else
        echo "$(_our_trigger_status || echo 'error')"
    fi
    ;;

*)
    echo "Usage: $0 <check|trigger|status> <repo> <pr_number>" >&2
    exit 1
    ;;
esac
