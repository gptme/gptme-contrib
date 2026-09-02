#!/usr/bin/env bash
# greptile-helper.sh — Safe Greptile trigger with anti-spam guards
#
# Usage:
#   greptile-helper.sh check <repo> <pr_number>   # Check status, exit 0=ok-to-trigger 1=skip
#   greptile-helper.sh trigger <repo> <pr_number> # Trigger once if safe, else skip
#   greptile-helper.sh status <repo> <pr_number>  # Print status string:
#     already-reviewed | needs-re-review | in-progress | awaiting-initial-review | stale | backoff | error
#
# Exit codes for 'check':
#   0 = safe to trigger (re-review needed: new commits since the last Greptile review)
#   1 = skip: no review yet (awaiting Greptile auto-review), or re-review trigger in-flight
#   2 = skip: reviewed by greptile-apps[bot], and no new commits since review (or ceiling hit)
#   3 = api error (fail-safe = skip)
#
# Erik's requests (ErikBjare/bob#434):
#   1. Reduce 30min age guard → 15min (reviews complete in 5-15min)
#   2. Re-request after addressing feedback once new commits land after a Greptile review
#
# Initial review policy: Greptile automatically reviews new PRs (drafts included —
# verified 2026-08-08 across gptme/gptme, gptme-contrib and gptme-cloud: bot reviews
# land 1-9 min after a draft PR opens, with no trigger comment). So we do not race it;
# re-reviews (new commits after the latest Greptile review) are the normal trigger path.
#
# Exception: auto-review is not guaranteed. On 2026-08-06/07 Greptile stopped reviewing
# gptme-contrib entirely (#1380-#1383, ~9h) while still reviewing other repos in minutes,
# leaving every affected PR permanently self-merge ineligible. So after
# GREPTILE_INITIAL_GRACE_MINS (default 45) with no review, `trigger` posts exactly one
# initial-review request. See the fallback block in the `trigger` command.
#
# NOTE: `check` and `status` still encode the older never-trigger policy — `check` exits 1
# and `status` prints 'awaiting-initial-review' for ALL unreviewed PRs regardless of age.
# Callers that gate on them therefore never reach the fallback: pr-greptile-trigger.py
# (ACTIONABLE_STATES = {"stale", "needs-re-review"}) and project-monitoring-lib.sh's
# cross-repo `none|stale` case both skip. The fallback is reached via the self-merge path,
# which calls `trigger` directly when self-merge-check.py reports "Greptile review not
# found", and via direct manual invocation.
#
# Root cause of spam incidents:
#   Multiple concurrent sessions each check "any trigger comments?" → all see 0
#   (due to API latency or concurrent execution) → all trigger.
#   Bot ack check is more reliable: Greptile reacts within ~5-10s of trigger.

set -euo pipefail

REPO="${2:-}"
PR_NUMBER="${3:-}"
TRIGGER_GRACE_SECONDS="${TRIGGER_GRACE_SECONDS:-900}"
ACK_GRACE_SECONDS="${ACK_GRACE_SECONDS:-7200}"  # 2h — Greptile can be slow; only flag as stuck after this long
MAX_RE_TRIGGERS="${MAX_RE_TRIGGERS:-3}"  # Max re-review triggers per review cycle before backing off
# Hard ceiling on TOTAL trigger comments we've ever posted on a PR, independent of
# review cycles. MAX_RE_TRIGGERS resets to 0 every time Greptile posts a fresh review,
# so a PR stuck in a fix→re-review→trigger loop (e.g. a mergeable-but-human-gated
# product PR that keeps getting polish commits) can be triggered unboundedly — one per
# PM run, indefinitely. This cap counts our lifetime triggers and backs off + escalates
# once exceeded. Incident: 2026-06-16, cloud#401 hit 25 triggers, #2906 25, #408 19.
# Incident 2026-07-09: lowered from 8 → 5; multiple PRs hit 4+ triggers before the
# cap fired, which Erik flagged as spam. Lower cap + stale-bypass fix below.
# Restored to 8 on 2026-07-20 per Erik's request (gptme/gptme#3206 comment).
MAX_TOTAL_TRIGGERS="${MAX_TOTAL_TRIGGERS:-8}"
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

_no_new_commit_since_our_last_trigger() {
    # Returns 0 (true) when the PR head commit is NOT newer than our most recent
    # `@greptileai review` trigger IN THE CURRENT REVIEW CYCLE — i.e. we already
    # triggered for the current head and nothing has been pushed since. Re-triggering
    # then is exactly the "re-reviewing without pushing anything" spam Erik flagged on
    # gptme#2908: the SHA-based _needs_re_review stays true forever when Greptile acks a
    # trigger but never advances its review commit_id to head, so without this guard each
    # grace window re-fires.
    #
    # IMPORTANT: Only triggers AFTER review_cutoff are in-scope (current review cycle).
    # Pre-review triggers are "spent" — Greptile already responded to them. When Greptile
    # updates its review comment in-place (advancing reviewed_at), old triggers become spent
    # and must NOT gate the next re-review. Root cause: gptme/gptme#2987 commit e07b1a4b
    # had committer.date 10s before our last trigger (written locally, then pushed). The
    # pre-cutoff trigger made last_trigger_date land after the commit date, so the guard
    # incorrectly suppressed a legitimate re-review. Fix: compare only within-cycle triggers.
    #
    # Fail-OPEN to 1 (allow) when there's no in-cycle trigger or head can't be read, so a
    # transient API failure never permanently suppresses a legitimate re-review.
    local review_cutoff="${1:-}"
    local head_date last_trigger_date
    local cycle_filter=""
    if [ -n "$review_cutoff" ]; then
        cycle_filter="| select(.created_at > \"$review_cutoff\")"
    fi
    head_date=$(gh api --paginate "repos/$REPO/pulls/$PR_NUMBER/commits" 2>/dev/null \
        | jq -rs '[.[][] | .commit.committer.date] | sort | last // ""' 2>/dev/null) || head_date=""
    last_trigger_date=$(_issue_comments_json \
        | jq -r "[.[][] | select(.user.login == \"$GITHUB_AUTHOR\" and (.body | test(\"@greptileai review\"))) $cycle_filter | .created_at] | sort | last // \"\"" 2>/dev/null) || last_trigger_date=""
    [ -z "$last_trigger_date" ] && return 1   # no in-cycle trigger → allow (nothing to gate against)
    [ -z "$head_date" ] && return 1           # can't read head → allow (fail-open)
    if _timestamp_gt "$head_date" "$last_trigger_date" 2>/dev/null; then
        return 1   # a commit landed AFTER our in-cycle trigger → re-review is legitimate
    fi
    return 0       # nothing pushed since our in-cycle trigger → suppress
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
_ISSUE_COMMENTS_CACHE_FILE="${TMPDIR:-/tmp}/greptile-issue-comments-$$.json"
_ISSUE_COMMENTS_ERROR_FILE="${TMPDIR:-/tmp}/greptile-issue-comments-error-$$"
trap 'rm -f "$_REVIEW_CACHE_FILE" "$_ISSUE_COMMENTS_CACHE_FILE" "$_ISSUE_COMMENTS_ERROR_FILE"' EXIT

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
_issue_comments_json() {
    # Cache the paginated issue-comments payload once per process; several guards
    # derive different fields from the same endpoint and should not each spend a
    # separate full traversal of the PR comment history.
    trap - EXIT
    if [ -f "$_ISSUE_COMMENTS_CACHE_FILE" ]; then
        cat "$_ISSUE_COMMENTS_CACHE_FILE"
        return
    fi
    if ! gh api "repos/$REPO/issues/$PR_NUMBER/comments" --paginate 2>/dev/null \
        | jq -s '.' > "$_ISSUE_COMMENTS_CACHE_FILE" 2>/dev/null; then
        echo '[]' > "$_ISSUE_COMMENTS_CACHE_FILE"
        : > "$_ISSUE_COMMENTS_ERROR_FILE"
    fi
    cat "$_ISSUE_COMMENTS_CACHE_FILE"
}

_total_trigger_count() {
    # Count our actual `@greptileai review` trigger commands over the PR lifetime.
    # This count backs the helper's spam ceiling, so another maintainer's manual
    # trigger must not consume one of our slots.
    local count
    count=$(_issue_comments_json \
        | jq -r '[.[][] | select(.user.login == "'"${GITHUB_AUTHOR}"'" and (.body | test("^@greptileai review( comment)?(\\s|$)")))] | length' 2>/dev/null) || count=0
    printf '%s\n' "${count:-0}"
}

_any_trigger_count() {
    # Initial-review deduplication is per PR, not per author: a maintainer's
    # manual trigger must suppress our fallback too.
    local count
    count=$(_issue_comments_json \
        | jq -r '[.[][] | select(.body | test("^@greptileai review( comment)?(\\s|$)"))] | length' 2>/dev/null) || count=0
    printf '%s\n' "${count:-0}"
}

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
                "score": (.body | [capture("Score[*:]*\\s*(?<n>[0-9])/5")] | if length == 0 then null else .[0].n | tonumber end)
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

# --- Helper: extract the head-SHA marker from a trigger comment body ---
# Trigger comments embed the PR head SHA they were posted for (see trigger command):
#   @greptileai review\n\n<!-- greptile-helper head-sha: <sha> -->
# Reads a comment JSON object on stdin, prints the marker SHA or "".
_marker_sha_from_body() {
    # Reset EXIT trap — runs in a $() subshell that inherits the parent's trap.
    trap - EXIT
    jq -r '.body // "" | [capture("greptile-helper head-sha: (?<s>[0-9a-fA-F]{7,40})")] | if length == 0 then "" else .[0].s end' 2>/dev/null
}

# --- Helper: our most recent @greptileai trigger comment (JSON object or {}) ---
_our_last_trigger_json() {
    trap - EXIT
    _issue_comments_json | jq "[.[][] | select(.user.login == \"$GITHUB_AUTHOR\" and (.body | test(\"@greptileai review\")))] | sort_by(.created_at) | last // {}" 2>/dev/null || echo "{}"
}

# --- Helper: sha named by the Greptile summary's "Last reviewed commit" footer ---
# Greptile does not always post a formal PR review object; on many PRs the only
# artifact is an issue comment whose footer names the commit it actually read.
# That footer is the same provenance self-merge-check.py gates on, so the helper
# must read it from the same place or the two tools disagree about staleness.
# Delegates to the sibling greptile-merge-signal.py rather than re-implementing
# its parser here — one regex, already covered by
# tests/test_greptile_merge_signal_provenance.py.
# Prints the sha, or nothing when there is no summary/footer (caller falls back).
_greptile_summary_reviewed_sha() {
    local signal_py out
    signal_py="$(dirname "${BASH_SOURCE[0]}")/greptile-merge-signal.py"
    [ -f "$signal_py" ] || return 0
    # The self-merge gate owns the enable/disable policy for the merge signal;
    # don't let that tool's disable flag silently drop provenance here (same
    # reasoning as self-merge-check.py, which pops the var before invoking).
    # NOTE: a non-zero exit here is NOT a failure. greptile-merge-signal.py uses
    # its exit code to report *merge eligibility* (e.g. exit 1 = "safe to merge"
    # marker absent), and still prints a complete JSON payload. Gating on the
    # exit status would silently drop provenance for every PR that is merely
    # ineligible — which is most of the PRs this staleness check exists to serve.
    out=$(env -u GREPTILE_MERGE_SIGNAL_DISABLED python3 "$signal_py" \
        --repo "$REPO" "$PR_NUMBER" 2>/dev/null) || true
    [ -n "$out" ] || return 0
    printf '%s' "$out" | jq -r '.reviewed_commit // ""' 2>/dev/null || true
}

# --- Helper: check if re-review is needed (new commits since latest review) ---
# Returns 0 = re-review needed, 1 = no re-review needed
#
# PRIMARY (robust): compare the commit Greptile actually reviewed (the latest
# greptile PR-review's commit_id) against the current PR head SHA. This is immune
# to the timestamp/timezone and in-place comment-update quirks the date heuristic
# below missed — e.g. ErikBjare/bob#890 (2026-06-14): a fix commit landed after
# the review but the date compare skipped the re-trigger, leaving a hardened PR
# stuck on a stale low score. A SHA mismatch is unambiguous and loop-safe: once
# Greptile reviews the head, reviewed_sha == head_sha → it stops re-triggering.
# On SHA mismatch, one more server-side check runs before declaring "re-review
# needed": Greptile often responds to triggers via in-place issue-comment update
# (never advancing the formal review's commit_id) — see the marker check inline.
#
# FALLBACK (date-based): when Greptile posted only an issue comment and no PR
# review object carries a commit_id, fall back to the original committer.date
# heuristic so behaviour is unchanged for that case.
_needs_re_review() {
    local reviewed_sha head_sha info reviewed_at new_commits
    reviewed_sha=$(gh api "repos/$REPO/pulls/$PR_NUMBER/reviews" --paginate 2>/dev/null \
        | jq -rs '[.[][]
              | select((.user.login // "") | test("greptile"; "i"))
              | select((.commit_id // "") != "")]
            | sort_by(.submitted_at) | last | (.commit_id // "")' 2>/dev/null) || reviewed_sha=""

    # No formal review object carries a commit_id (Greptile frequently reviews via
    # issue comment only). Before falling back to the date heuristic, use the
    # summary footer's "Last reviewed commit" — it is server-side, unambiguous,
    # and is exactly what self-merge-check.py gates on. Without this the two tools
    # deadlock: the gate demands a re-review of head while this helper reports
    # "no new commits" and refuses to trigger one. Observed on
    # gptme/gptme-cloud#892 (2026-09-02): head dcf167ea was committed at 12:08:15
    # and Greptile posted its review of the PREVIOUS commit 63f9d6b7 at 12:08:47,
    # so `committer.date > reviewed_at` counted zero new commits and the PR sat
    # permanently ineligible while repo-wide CI stayed red.
    if [ -z "$reviewed_sha" ]; then
        reviewed_sha=$(_greptile_summary_reviewed_sha) || reviewed_sha=""
    fi

    if [ -n "$reviewed_sha" ]; then
        head_sha=$(gh api "repos/$REPO/pulls/$PR_NUMBER" --jq '.head.sha // ""' 2>/dev/null) || head_sha=""
        if [ -n "$head_sha" ]; then
            if [ "$reviewed_sha" = "$head_sha" ]; then
                return 1  # Formal PR review is on the current head — no re-review needed.
            fi
            # SHA mismatch: Greptile's formal PR review is on a stale commit. However,
            # Greptile often responds to re-review triggers via in-place issue comment
            # update rather than a new formal PR review, so the formal review's commit_id
            # can stay stale forever while Greptile is in fact up to date.
            # Root cause of #1246 spam (3 triggers in 2.5h, 2026-07-09): SHA mismatch kept
            # returning "re-review needed" indefinitely, while in-place comment updates
            # advanced review_cutoff past the in-cycle trigger, causing
            # _no_new_commit_since_our_last_trigger to fail open on every PM cycle.
            #
            # Detection uses server-side data ONLY: our trigger comments embed the head
            # SHA they were posted for (see trigger command). If our latest trigger was
            # for the CURRENT head and Greptile's issue comment was updated after that
            # trigger, Greptile already processed this head — suppress the re-trigger.
            # Deliberately NOT a committer.date comparison: commit dates lag push time
            # (written locally, pushed later — gptme#2987), so a date compare masks
            # genuinely-new heads (Greptile P1 on #1247). SHA equality plus comment
            # timestamps are both server-side and unambiguous.
            # Legacy triggers without the marker fall through to "re-review needed"
            # (pre-#1247 behavior); the next marker-carrying trigger self-heals the PR.
            local _last_trigger _trig_sha _trig_created _sha_info _sha_reviewed_at
            _last_trigger=$(_our_last_trigger_json)
            _trig_sha=$(echo "$_last_trigger" | _marker_sha_from_body) || _trig_sha=""
            if [ -n "$_trig_sha" ] && [ "$_trig_sha" = "$head_sha" ]; then
                _trig_created=$(echo "$_last_trigger" | _json_field "created_at") || _trig_created=""
                _sha_info=$(_greptile_review_info) || _sha_info=""
                _sha_reviewed_at=$(echo "$_sha_info" | _json_field "reviewed_at") || _sha_reviewed_at=""
                if [ -n "$_trig_created" ] \
                    && [ -n "$_sha_reviewed_at" ] && [ "$_sha_reviewed_at" != "null" ] \
                    && _timestamp_gt "$_sha_reviewed_at" "$_trig_created" 2>/dev/null; then
                    return 1  # Greptile responded (in-place) after our trigger for this exact head.
                fi
            fi
            return 0  # Re-review needed: formal review is stale, no confirmed response for this head.
        fi
    fi

    # Fallback: no PR-review commit_id available → use the date heuristic.
    info=$(_greptile_review_info) || return 1
    reviewed_at=$(echo "$info" | _json_field "reviewed_at") || reviewed_at=""
    # Defense-in-depth: jq outputs literal "null" on null JSON via -r flag.
    # _json_field uses Python (handles None correctly), but guard against
    # future refactors that switch to jq-based extraction.
    if [ "$reviewed_at" = "null" ]; then reviewed_at=""; fi
    if [ -z "$reviewed_at" ]; then
        return 1
    fi
    new_commits=$(gh api "repos/$REPO/pulls/$PR_NUMBER/commits" --paginate \
        2>/dev/null | jq -s "[.[][] | select(.commit.committer.date > \"$reviewed_at\")] | length" \
        2>/dev/null) || new_commits="0"
    [ "${new_commits:-0}" -gt 0 ]
}

# --- Helper: check our last trigger comment + its reactions ---
# Returns: "none" | "in-progress" | "stale" | "stale-acked"
# "in-progress"  = recent trigger (< 15min), or Greptile acked it within ACK_GRACE_SECONDS and hasn't reviewed yet
# "stale"        = trigger was never acked by Greptile; old (> TRIGGER_GRACE_SECONDS). Apply no-new-commit guard.
# "stale-acked"  = Greptile acked our trigger but never posted a review after ACK_GRACE_SECONDS. Truly stuck;
#                  callers should bypass the no-new-commit guard to unstick it.
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
            # When review_cutoff is EMPTY there is no prior Greptile review at all,
            # so there is only one cycle and any timestamp in the file is in it by
            # definition. The fast-path must apply there too: the trigger command
            # also writes this file on the initial-review fallback path, and skipping
            # the guard for an empty cutoff would let two sequential runs both miss
            # the (not-yet-propagated) comment via the API and both post.
            local _ts_in_cycle=0  # 1 = TS is from current review cycle; 0 = skip fast-path
            if [ -n "$review_cutoff" ]; then
                if _timestamp_gt "$_local_ts" "$review_cutoff" 2>/dev/null; then
                    _ts_in_cycle=1
                fi
            else
                _ts_in_cycle=1
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
    comment_info=$(_issue_comments_json | jq '
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

    if [ "${bot_ack_count:-0}" -gt 0 ]; then
        if [ "${comment_age_seconds:-9999}" -lt "$ACK_GRACE_SECONDS" ]; then
            # Greptile acked and review is still in progress (within 2h grace window).
            echo "in-progress"
        else
            # Greptile acked our trigger (👀/+1) but never posted a review after a long
            # wait — truly stuck. Return "stale-acked" so callers can bypass the
            # no-new-commit guard and re-trigger to unstick it.
            echo "stale-acked"
        fi
    else
        # Greptile never acked this trigger. Return plain "stale" so callers still
        # apply the no-new-commit guard — only re-trigger if a new commit exists.
        # Without this distinction, every PM session firing 15+ min after the last
        # trigger re-triggers even when nothing was pushed (root cause: 2026-07-09,
        # gptme-contrib#1246 got 4 triggers in <3h with no new commits).
        echo "stale"
    fi
}

# --- Main commands ---
case "${1:-}" in
check)
    # Check if safe to trigger
    if _has_greptile_review; then
        # Already reviewed — check if re-review is needed (new commits since review)
        if _needs_re_review; then
            # Hard lifetime ceiling: if hit, skip just like "no new commits" (exit 2)
            _total_triggers=$(_total_trigger_count)
            if [ "${_total_triggers:-0}" -ge "$MAX_TOTAL_TRIGGERS" ]; then
                echo "  [greptile] BACKOFF: $REPO#$PR_NUMBER has $_total_triggers lifetime triggers (cap $MAX_TOTAL_TRIGGERS). Skipping." >&2
                exit 2  # Ceiling hit — skip (same as "already reviewed, nothing to do")
            fi
            reviewed_at=$( _greptile_review_info | _json_field "reviewed_at") || reviewed_at=""
            # Check trigger status BEFORE the no-new-commit guard. If stale-acked (Greptile
            # acked but never posted a review after a long wait), allow re-trigger even
            # without new commits so the acked-but-not-reviewed failure mode (gptme#1651)
            # can escape. Plain "stale" (never acked) still applies the no-new-commit guard.
            trigger_status=$(_our_trigger_status "$reviewed_at" || echo "in-progress")
            if [ "$trigger_status" = "in-progress" ]; then
                exit 1  # Re-review trigger in-flight
            fi
            # Root guard: no new commit since our last in-cycle trigger → not safe to trigger.
            # ONLY bypass for "stale-acked" (Greptile acked but never reviewed = truly stuck).
            # "stale" (never acked) must NOT bypass this guard: re-trigger only with new commits.
            # Root cause of 2026-07-09 spam (gptme-contrib#1246): "stale" (no ack, old trigger)
            # bypassed this guard, causing re-triggers every 15+ min with zero new commits.
            if [ "$trigger_status" != "stale-acked" ] && _no_new_commit_since_our_last_trigger "$reviewed_at"; then
                exit 2  # Nothing pushed since our last in-cycle trigger — treat as up-to-date.
            fi
            exit 0  # Re-review needed
        fi
        exit 2  # Reviewed and no new commits since latest review
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
            # Hard total-trigger ceiling (does NOT reset per review cycle). A PR that keeps
            # accruing commits → re-reviews can otherwise be triggered forever. Past this cap,
            # the PR is pathological (stuck loop, or mergeable-but-human-gated) — stop
            # triggering and escalate to a human instead of adding more spam.
            _total_triggers=$(_total_trigger_count)
            if [ "${_total_triggers:-0}" -ge "$MAX_TOTAL_TRIGGERS" ]; then
                echo "  [greptile] BACKOFF: $REPO#$PR_NUMBER already has $_total_triggers lifetime triggers (cap $MAX_TOTAL_TRIGGERS). Not re-triggering — this PR is stuck or human-gated; escalate to merge/close/intervene."
                exit 0
            fi
            reviewed_at=$( _greptile_review_info | _json_field "reviewed_at") || reviewed_at=""
            # Check trigger status BEFORE the no-new-commit guard. If stale-acked (Greptile
            # acked but never posted a review after a long wait), allow re-trigger even
            # without new commits so the acked-but-not-reviewed failure mode (gptme#1651)
            # can escape. Plain "stale" (never acked) still applies the no-new-commit guard.
            trigger_status=$(_our_trigger_status "$reviewed_at" || echo "in-progress")
            if [ "$trigger_status" = "in-progress" ]; then
                echo "  [greptile] Re-review trigger in-flight on $REPO#$PR_NUMBER. Skipping."
                exit 0
            fi
            # ROOT GUARD: never re-review without a new commit since OUR last in-cycle trigger.
            # _needs_re_review (SHA-based) stays true when Greptile acks but doesn't advance
            # its review commit_id to head, so this catches the "re-reviewing without pushing"
            # spam (gptme#2908). Exception: trigger_status=stale-acked means Greptile acked
            # our trigger but never posted a review — skip the guard to escape that stuck state.
            # Plain "stale" (trigger was never acked) must NOT bypass the guard — only re-trigger
            # when new commits exist. Root cause of 2026-07-09 spam (gptme-contrib#1246):
            # "stale" (no ack) bypassed this guard, causing triggers every 15+ min with no pushes.
            if [ "$trigger_status" != "stale-acked" ] && _no_new_commit_since_our_last_trigger "$reviewed_at"; then
                echo "  [greptile] SKIP: $REPO#$PR_NUMBER has no new commits since our last @greptileai review trigger. Not re-reviewing (nothing was pushed)."
                exit 0
            fi
            echo "  [greptile] Re-triggering @greptileai review on $REPO#$PR_NUMBER (new commits landed after the last review)..."
            # Embed the current head SHA so _needs_re_review can later tell whether an
            # in-place Greptile comment update was a response to a trigger for THIS head
            # (SHA equality instead of committer.date heuristics — see gptme#2987).
            # If the head can't be read, post a plain trigger: the marker is an
            # optimization, and _needs_re_review treats marker-less triggers as legacy.
            _head_sha=$(gh api "repos/$REPO/pulls/$PR_NUMBER" --jq '.head.sha // ""' 2>/dev/null) || _head_sha=""
            _trigger_body="@greptileai review"
            if [ -n "$_head_sha" ]; then
                _trigger_body="$_trigger_body

<!-- greptile-helper head-sha: $_head_sha -->"
            fi
            # Use REST API instead of `gh pr comment` (GraphQL) — REST has a
            # separate 5000/hour quota that's rarely exhausted.
            if BOB_GREPTILE_HELPER=1 gh api "repos/$REPO/issues/$PR_NUMBER/comments" -f body="$_trigger_body" --silent 2>/dev/null; then
                # Record trigger timestamp locally — fast-path guard against GitHub API
                # propagation delay that causes sequential callers to see "no trigger"
                # and re-trigger. See: 2026-03-19 INCIDENT #5.
                if ! date -u +%Y-%m-%dT%H:%M:%SZ > "$_TRIGGER_TS_FILE" 2>/dev/null; then
                    echo "  [greptile] Warning: could not write trigger-timestamp file; propagation-delay guard disabled for this trigger."
                fi
                echo "  [greptile] Re-triggered successfully."
            else
                echo "  [greptile] Trigger failed (non-fatal)."
            fi
        else
            echo "  [greptile] Already reviewed on $REPO#$PR_NUMBER (no new commits since latest review). Skipping."
        fi
        exit 0
    fi

    # No review yet. Normally let Greptile auto-review: manually triggering the
    # first review races the bot and produces two reviews.
    #
    # But auto-review is not guaranteed to arrive, and this branch used to be an
    # unconditional dead end. Observed 2026-08-06/07: Greptile stopped
    # auto-reviewing gptme-contrib entirely (#1380-#1383, zero reviews across
    # ~9h) while still reviewing gptme and aw-server-rust within ~2 minutes. With
    # no path to a first review, every affected PR is permanently self-merge
    # ineligible, because the self-merge gate requires a Greptile review — and
    # the symptom reads as "PRs aren't ready" rather than "the reviewer never came".
    #
    # So after a grace period, trigger once. Every existing protection still
    # applies: the flock above, the local in-flight check, the lifetime cap, and
    # the trigger-timestamp record.
    _initial_grace="${GREPTILE_INITIAL_GRACE_MINS:-45}"
    case "$_initial_grace" in
        ''|*[!0-9]*)
            echo "  [greptile] Invalid GREPTILE_INITIAL_GRACE_MINS=$_initial_grace; using 45 minutes." >&2
            _initial_grace=45
            ;;
    esac

    # "trigger once" means exactly once. Two guards, in order:
    #
    #   1. A fresh local trigger timestamp means a post is in flight but may not
    #      be visible through GitHub's comments API yet (INCIDENT #5). The flock
    #      handles concurrent runs; this guard handles sequential runs. Do not
    #      reuse _our_trigger_status here: it broadly matches any of our comments
    #      containing "greptileai", so fresh review-feedback prose can delay the
    #      fallback even though no trigger was posted.
    #   2. lifetime trigger count >= 1 — we already posted our one `@greptileai review`
    #      on this PR. The re-review path deliberately permits later attempts after
    #      new commits or a stuck acknowledgement; this path has no such escape hatch.
    _initial_ts_in_flight=0
    if [ -f "$_TRIGGER_TS_FILE" ]; then
        _local_ts=$(cat "$_TRIGGER_TS_FILE" 2>/dev/null || true)
        if [ -n "$_local_ts" ]; then
            _local_age=$(_age_seconds "$_local_ts" 2>/dev/null) || _local_age=9999
            if [ "${_local_age:-9999}" -lt "$TRIGGER_GRACE_SECONDS" ]; then
                _initial_ts_in_flight=1
            fi
        fi
    fi
    if [ "$_initial_ts_in_flight" -eq 1 ]; then
        echo "  [greptile] Initial-review trigger already in flight on $REPO#$PR_NUMBER (local timestamp is fresh). Not triggering again."
        exit 0
    fi
    _total_triggers=$(_total_trigger_count)
    _any_triggers=$(_any_trigger_count)
    if [ -f "$_ISSUE_COMMENTS_ERROR_FILE" ]; then
        echo "  [greptile] Could not read PR comments for $REPO#$PR_NUMBER. Refusing to trigger an initial review without duplicate-check data."
        exit 3
    fi
    # The exactly-once guard below is stricter than the general lifetime cap,
    # but keep the cap explicit: it remains a hard backstop if that policy is
    # relaxed later and makes the protection promised by this helper auditable.
    if [ "${_total_triggers:-0}" -ge "$MAX_TOTAL_TRIGGERS" ]; then
        echo "  [greptile] BACKOFF: $REPO#$PR_NUMBER has $_total_triggers helper trigger(s) (cap $MAX_TOTAL_TRIGGERS). Not triggering an initial review — escalate to a human."
        exit 0
    fi
    if [ "${_any_triggers:-0}" -ge 1 ]; then
        echo "  [greptile] Initial-review trigger already attempted on $REPO#$PR_NUMBER ($_any_triggers trigger comment(s), any author). Not triggering again — escalate to a human if Greptile never reviews."
        exit 0
    fi

    _pr_info=$(gh api "repos/$REPO/pulls/$PR_NUMBER" --jq '{state: (.state // ""), created_at: (.created_at // "")}' 2>/dev/null) || _pr_info=""
    _pr_state=$(printf '%s' "$_pr_info" | _json_field "state") || _pr_state=""
    _created=$(printf '%s' "$_pr_info" | _json_field "created_at") || _created=""
    if [ "$_pr_state" != "open" ]; then
        echo "  [greptile] PR $REPO#$PR_NUMBER is ${_pr_state:-unavailable}, not open. Skipping initial-review trigger."
        exit 0
    fi
    _age_seconds=$(_age_seconds "$_created" 2>/dev/null) || _age_seconds=""
    if [ -z "$_age_seconds" ]; then
        echo "  [greptile] Could not parse PR creation timestamp for $REPO#$PR_NUMBER. Refusing to trigger an initial review."
        exit 3
    fi
    _age_mins=$(( _age_seconds / 60 ))

    if [ "$_age_mins" -lt "$_initial_grace" ]; then
        echo "  [greptile] No review yet on $REPO#$PR_NUMBER (${_age_mins}m old, grace ${_initial_grace}m). Awaiting Greptile auto-review."
        exit 0
    fi

    echo "  [greptile] No auto-review on $REPO#$PR_NUMBER after ${_age_mins}m (grace ${_initial_grace}m) — triggering initial review..."
    _head_sha=$(gh api "repos/$REPO/pulls/$PR_NUMBER" --jq '.head.sha // ""' 2>/dev/null) || _head_sha=""
    _trigger_body="@greptileai review"
    if [ -n "$_head_sha" ]; then
        _trigger_body="$_trigger_body

<!-- greptile-helper head-sha: $_head_sha -->"
    fi
    # Record the intent BEFORE posting, and refuse to post if it cannot be
    # recorded.
    #
    # Writing the timestamp after a successful post leaves a window with no
    # guard at all: if the write fails (read-only TMPDIR, full disk), the next
    # invocation inside TRIGGER_GRACE_SECONDS finds no TS file, and the GitHub
    # comments API can take minutes to surface the comment we just made — so
    # `_our_trigger_status` says "none" and we post a second `@greptileai
    # review`. That is INCIDENT #5 (2026-03-19) exactly, and a warning saying
    # "the guard is disabled" does not prevent it.
    #
    # So: write first, and treat a failed write as fatal for this trigger. Not
    # triggering costs one cycle; double-triggering is the incident. If the post
    # fails, keep the provisional timestamp for the bounded grace window. A failed
    # client response is ambiguous: GitHub may have committed the comment before a
    # timeout, and deleting the guard would let the next invocation duplicate it
    # before the comments API catches up.
    if ! date -u +%Y-%m-%dT%H:%M:%SZ > "$_TRIGGER_TS_FILE" 2>/dev/null; then
        echo "  [greptile] Could not write trigger-timestamp file ($_TRIGGER_TS_FILE); refusing to trigger without the propagation-delay guard. Retrying next cycle."
        exit 0
    fi
    if BOB_GREPTILE_HELPER=1 gh api "repos/$REPO/issues/$PR_NUMBER/comments" -f body="$_trigger_body" --silent 2>/dev/null; then
        echo "  [greptile] Initial review triggered."
    else
        echo "  [greptile] Trigger failed or its result was ambiguous; retaining the propagation-delay guard for this grace window."
    fi
    exit 0
    ;;

status)
    if _has_greptile_review; then
        if _needs_re_review; then
            # Hard lifetime ceiling: report "backoff" so callers/dashboards see the true state
            _total_triggers=$(_total_trigger_count)
            if [ "${_total_triggers:-0}" -ge "$MAX_TOTAL_TRIGGERS" ]; then
                echo "backoff"
            else
                reviewed_at=$(_greptile_review_info | _json_field "reviewed_at") || reviewed_at=""
                # Check trigger status BEFORE the no-new-commit guard (mirrors check/trigger ordering).
                trigger_status=$(_our_trigger_status "$reviewed_at" || echo "in-progress")
                if [ "$trigger_status" = "in-progress" ]; then
                    echo "in-progress"
                # Root guard: no new commit since our last in-cycle trigger → report as up-to-date.
                # Exception: stale-acked means Greptile acked but never reviewed → needs-re-review.
                # "stale-acked" is the internal name; the status command normalizes it to "stale"
                # for callers (pr-greptile-trigger.py checks for ACTIONABLE_STATES = {"stale", ...}).
                elif [ "$trigger_status" != "stale-acked" ] && _no_new_commit_since_our_last_trigger "$reviewed_at"; then
                    echo "already-reviewed"
                elif [ "$trigger_status" = "stale-acked" ]; then
                    echo "stale"
                else
                    echo "needs-re-review"
                fi
            fi
        else
            echo "already-reviewed"
        fi
    else
        # Keep the public status contract stable for unreviewed PRs. The trigger
        # command itself owns in-flight deduplication via the timestamp and comment
        # guards above; callers use this state to distinguish initial-review waits
        # from re-review work.
        echo "awaiting-initial-review"
    fi
    ;;

*)
    echo "Usage: $0 <check|trigger|status> <repo> <pr_number>" >&2
    exit 1
    ;;
esac
