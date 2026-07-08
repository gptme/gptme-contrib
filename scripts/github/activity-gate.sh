#!/usr/bin/env bash
# GitHub activity gate — lightweight pre-check for monitoring scripts.
#
# Checks GitHub for actionable activity (PR updates, CI failures, notifications)
# using state-tracked timestamps to avoid re-reporting old items.
#
# Exit codes:
#   0 = actionable work found (work items printed to stdout)
#   1 = no actionable work found (nothing printed)
#   2 = usage error
#
# Usage:
#   activity-gate.sh --author AUTHOR --org ORG [--repo EXTRA_REPO]... [--state-dir DIR]
#
# Examples:
#   # Check gptme org + specific repos
#   activity-gate.sh --author TimeToBuildBob --org gptme --repo ErikBjare/bob
#
#   # Use as a gate before spawning an LLM session
#   if work=$(./activity-gate.sh --author MyBot --org myorg); then
#       echo "Work found, spawning session..."
#       echo "$work"
#   else
#       echo "Nothing to do."
#   fi
#
# Design notes & gotchas:
#
#   State tracking: Each check type uses files in STATE_DIR to remember what
#   it last saw. On first run, all items are seeded (state created) but NOT
#   reported — only changes after the first run trigger output.
#
#   PR update filtering: Not all updatedAt bumps are worth a session.
#   We skip: (1) the AUTHOR's own comments (self-triggered), and
#   (2) comments that @-mention someone other than AUTHOR (e.g. "@greptileai
#   review" — human talking to a bot, not to us). Bot reviews (Greptile etc.)
#   ARE allowed through — they contain actionable feedback.
#
#   GitHub comment types: `gh pr list --json comments` returns issue-style
#   comments only (the general discussion thread). Inline review comments
#   (left on specific lines of code) only appear in `latestReviews`. Both
#   can bump updatedAt, so we check both and compare timestamps.
#
#   CI failure dedup: Tracked by a hash of all check conclusions. Re-triggers
#   only when CI state actually changes (e.g. new failure, or failure resolves).
#   Caveat: pending/in-progress checks have empty conclusions that change when
#   they complete, causing a state change even if the final result is the same.
#
#   Merge conflicts: NOT state-tracked (intentionally). A conflicting PR should
#   nag every run until resolved.
#
#   Assigned issues: resolution is keyed on the LAST ACTOR, not the timestamp
#   watermark used elsewhere. An assigned issue keeps surfacing until AUTHOR is
#   the last commenter — so it does NOT follow the "seed on first sight, don't
#   report" rule above (first sight with someone else holding the ball emits
#   immediately). This self-heals dropped sessions: a watermark would mark the
#   issue handled the moment it emitted, so one missed session silenced it
#   forever. Re-nag cooldown (1h) prevents flooding. Costs 1 REST call per open
#   assigned issue to read the last commenter.
#
#   Greptile score sweep: Proactively finds PRs with low Greptile scores (< 5/5)
#   that need code fixes. Greptile updates comments in-place, so updatedAt never
#   bumps — without this sweep, low-scored PRs sit indefinitely. State-tracked
#   by score + HEAD SHA with 1-hour cooldown. Costs 1 extra REST API call per
#   open PR (issue comments endpoint). HEAD SHA comes from fetch_pr_data().
#
#   Notifications: Filters for actionable reasons (review_requested, mention,
#   assign, author, comment). State-tracked by notification ID + updated_at:
#   follow-ups on the same thread reuse the same ID but advance updated_at, so
#   the gate re-emits when updated_at advances. State files store the last-seen
#   updated_at timestamp. State files accumulate in STATE_DIR/notif-*.state.
#   GitHub notifications clear when marked as read upstream, so old state files
#   become inert.
#
#   Item grouping: This gate emits one item per event (a PR can produce separate
#   pr_update, ci_failure, and merge_conflict items). Callers that dispatch
#   per-item sessions should group items by repo#number before dispatching to
#   avoid redundant sessions investigating the same PR/issue independently.
#
#   Merge readiness: Finds PRs with CLEAN mergeStateStatus, MERGEABLE status,
#   and acceptable Greptile score (>= 5 or no review). Unlike other checks,
#   first-time discovery DOES emit immediately — merge-ready PRs are actionable.
#   State-tracked with 12-hour cooldown; re-emits on HEAD SHA change.
#
#   API efficiency: cached PR data is fetched once per repo via fetch_pr_data()
#   and shared across the state-tracked checks (PR updates, CI failures,
#   Greptile sweep). Merge-sensitive checks (merge conflicts, merge-ready) use
#   a short-TTL fetch (GH_CACHE_TTL_LIVE_PR, default 180s) instead of bypassing
#   cache entirely, keeping merge-conflict detection fresh within ~4 minutes.
#   This reduces gh pr list calls from 3N to ~1.5N on average (where N = number
#   of repos), while cutting uncached live-PR calls by ~50%.
#   The repo list from discover_repos() is cached for 1 hour.
#
#   Parallelism: Per-repo work runs concurrently (up to 8 repos at once).
#   State files are repo-prefixed, so there's no cross-repo contention.
#   Results are collected via temp files. On a 13-repo org, this reduces
#   wall-clock time from ~27s to ~7s.
#
#   Subshell note: Several checks pipe into `while read` loops, which run in
#   subshells. Variable modifications inside these loops don't propagate to the
#   parent. This is fine because output is captured via stdout, not variables.

set -euo pipefail

# --- Parse args ---
AUTHOR=""
ORG=""
EXTRA_REPOS=()
STATE_DIR="/tmp/github-activity-gate-state"
FORMAT="markdown"  # markdown (human-readable) or jsonl (one JSON object per work item)

while [[ $# -gt 0 ]]; do
    case $1 in
        --author) AUTHOR="$2"; shift 2 ;;
        --org) ORG="$2"; shift 2 ;;
        --repo) EXTRA_REPOS+=("$2"); shift 2 ;;
        --state-dir) STATE_DIR="$2"; shift 2 ;;
        --format) FORMAT="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 --author AUTHOR --org ORG [--repo EXTRA_REPO]... [--state-dir DIR] [--format FORMAT]"
            echo ""
            echo "Checks GitHub for actionable activity. Exits 0 with work items on stdout"
            echo "if activity found, exits 1 silently if nothing to do."
            echo ""
            echo "Options:"
            echo "  --author    GitHub username to check PRs/issues for (required)"
            echo "  --org       GitHub org to scan all repos from (required)"
            echo "  --repo      Additional repo to check (can be repeated)"
            echo "  --state-dir Directory for state tracking files (default: /tmp/github-activity-gate-state)"
            echo "  --format    Output format: 'markdown' (default) or 'jsonl' (one JSON object per item)"
            echo ""
            echo "JSONL format: {\"type\":\"pr_update|ci_failure|merge_ready|greptile_needs_fix|greptile_needs_improvement|assigned_issue|notification\","
            echo "               \"repo\":\"owner/repo\", \"number\":123, \"title\":\"...\", \"detail\":\"...\"}"
            exit 0
            ;;
        *) echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [ -z "$AUTHOR" ] || [ -z "$ORG" ]; then
    echo "Error: --author and --org are required" >&2
    exit 2
fi

if [ "$FORMAT" != "markdown" ] && [ "$FORMAT" != "jsonl" ]; then
    echo "Error: --format must be 'markdown' or 'jsonl'" >&2
    exit 2
fi

mkdir -p "$STATE_DIR"

# --- Functions ---

# Portable hash function (works on both Linux and macOS)
portable_hash() {
    if command -v md5sum &>/dev/null; then
        md5sum | cut -c1-16
    elif command -v shasum &>/dev/null; then
        shasum -a 256 | cut -c1-16
    else
        # Fallback: use cksum (POSIX, always available)
        cksum | cut -d' ' -f1
    fi
}

# Emit a work item in the configured format.
# Args: type repo number title detail
emit_item() {
    local type=$1 repo=$2 number=$3 title=$4 detail=${5:-}
    if [ "$FORMAT" = "jsonl" ]; then
        jq -cn --arg t "$type" --arg r "$repo" --argjson n "$number" --arg title "$title" --arg d "$detail" \
            '{type: $t, repo: $r, number: $n, title: $title, detail: $d}'
    else
        local label
        case "$type" in
            pr_update)         label="PR" ;;
            ci_failure)        label="CI FAIL" ;;
            assigned_issue)    label="Issue" ;;
            notification)      label="Notification" ;;
            master_ci_failure) label="MASTER CI" ;;
            merge_conflict)        label="CONFLICT" ;;
            greptile_needs_fix)    label="GREPTILE FIX" ;;
            greptile_needs_improvement) label="GREPTILE" ;;
            merge_ready)               label="MERGE READY" ;;
            *)                     label="$type" ;;
        esac
        echo "$repo — $label #$number: $title ($detail)"
    fi
}

discover_repos() {
    # Cache repo list for 1 hour by default — org membership rarely changes.
    # Override with GH_CACHE_TTL_REPO (seconds).
    local cache_file="$STATE_DIR/repo-list-${ORG}.cache"
    local cache_max_age="${GH_CACHE_TTL_REPO:-3600}"

    if [ -f "$cache_file" ]; then
        local cache_age
        local mtime
        mtime=$(stat -c %Y "$cache_file" 2>/dev/null || stat -f %m "$cache_file" 2>/dev/null || echo "")
        if [ -z "$mtime" ]; then
            echo "WARN: stat failed on cache file, skipping cache" >&2
            rm -f "$cache_file"
        else
            cache_age=$(( $(date +%s) - mtime ))
            if [ "$cache_age" -lt "$cache_max_age" ]; then
                cat "$cache_file"
                for r in "${EXTRA_REPOS[@]}"; do echo "$r"; done
                return
            fi
        fi
    fi

    local repos
    repos=$(gh repo list "$ORG" --limit 50 --json nameWithOwner --jq '.[].nameWithOwner' 2>/dev/null || true)
    if [ -n "$repos" ]; then
        echo "$repos" > "$cache_file"
    fi
    echo "$repos"
    for r in "${EXTRA_REPOS[@]}"; do echo "$r"; done
}

# Per-repo GraphQL response cache. Project monitoring runs every 2 minutes
# across 50 repos, so the old defaults implied roughly 750 PR-data fetches/hr
# plus 500/hr each for assigned-issue and master-CI fetches (~1,750/hr total).
# Raising the defaults to 480s / 600s / 600s drops those lanes to ~375 / 300 /
# 300 fetches/hr (~975/hr total) without changing cadence.
#
# Override via env: GH_CACHE_TTL_PR / GH_CACHE_TTL_ISSUE / GH_CACHE_TTL_RUN /
# GH_CACHE_TTL_LIVE_PR / GH_CACHE_LOCK_TIMEOUT (seconds).
# Set any to 0 to bypass the cache (useful for diagnostics).
GH_CACHE_DIR="${GH_CACHE_DIR:-$STATE_DIR/gh-cache}"
# Increased from 240→480 and 300→600 (2026-05-21) after GraphQL rate-limit regression.
# At a 2-minute cadence, the PR-data lane drops from ~750 fetches/hr to ~375/hr
# (75% cache hit instead of 50%).
# GH_CACHE_TTL_LIVE_PR (default 180s) added 2026-07-08: the live-PR fetch lane for
# merge-sensitive checks (conflicts, merge-ready, review state) was unbounded (TTL=0),
# costing 1 extra GraphQL call per repo with open PRs per monitoring cycle. With a
# 180s TTL and 2-minute cadence, repos with PRs still get a fresh fetch every ~4
# minutes while halving live-PR GraphQL calls (~75/hr per repo vs ~150/hr).
# Set to 0 to restore the original uncached behaviour (useful for debugging).
# See: tasks/github-graphql-rate-limit-regression.md
GH_CACHE_TTL_PR="${GH_CACHE_TTL_PR:-480}"
GH_CACHE_TTL_ISSUE="${GH_CACHE_TTL_ISSUE:-600}"
GH_CACHE_TTL_RUN="${GH_CACHE_TTL_RUN:-600}"
GH_CACHE_TTL_LIVE_PR="${GH_CACHE_TTL_LIVE_PR:-180}"
GH_CACHE_LOCK_TIMEOUT="${GH_CACHE_LOCK_TIMEOUT:-30}"

gh_cache_fetch_and_store() {
    local producer="$1" cache_file="$2" safe_key="$3" has_fallback="$4" fallback="${5-}"
    local out tmp_file
    if out=$(eval "$producer"); then
        if [ -n "$out" ]; then
            tmp_file=$(mktemp "$GH_CACHE_DIR/${safe_key}.json.tmp.XXXXXX" 2>/dev/null || printf '')
            if [ -n "$tmp_file" ]; then
                # Write via temp file + rename so readers never observe partial JSON.
                if ! printf '%s' "$out" > "$tmp_file" || ! mv "$tmp_file" "$cache_file"; then
                    rm -f "$tmp_file"
                fi
            fi
        fi
        printf '%s' "$out"
        return 0
    fi
    if [ "$has_fallback" = "1" ]; then
        printf '%s' "$fallback"
        return 0
    fi
    return 1
}

# Read cached value if fresh enough, else run the producer command and cache its
# stdout. The producer is passed as a single shell-evaluated string so callers
# can include flags and pipes.
#
# Thundering-herd prevention: when the cache is stale, a flock-based lock
# serializes concurrent callers. The first caller acquires the lock, fetches,
# and writes the cache. Subsequent callers, after the lock is released,
# find the cache fresh via double-checked locking and return immediately
# without making another GitHub API call. Requires util-linux flock (Linux).
#
# Args:
#   $1 — cache key (filesystem-safe; will be normalized)
#   $2 — TTL seconds (0 = no cache)
#   $3 — producer command (eval'd in subshell)
#   $4 — fallback stdout to emit on producer failure (optional; not cached)
gh_cache_get_or_fetch() {
    local key="$1" ttl="$2" producer="$3" fallback="${4-}"
    if [ "$ttl" -le 0 ]; then
        if eval "$producer"; then
            return 0
        fi
        if [ $# -ge 4 ]; then
            printf '%s' "$fallback"
            return 0
        fi
        return 1
    fi
    mkdir -p "$GH_CACHE_DIR" 2>/dev/null || true
    # Normalize key: replace slashes with `-`
    local safe_key="${key//\//-}"
    local cache_file="$GH_CACHE_DIR/${safe_key}.json"
    # Fast path: cache is fresh — return without acquiring a lock
    if [ -f "$cache_file" ]; then
        local mtime
        mtime=$(stat -c %Y "$cache_file" 2>/dev/null || stat -f %m "$cache_file" 2>/dev/null || echo "")
        if [ -n "$mtime" ]; then
            local age=$(( $(date +%s) - mtime ))
            if [ "$age" -lt "$ttl" ]; then
                cat "$cache_file"
                return 0
            fi
        fi
    fi
    # Slow path: cache is stale or missing. Serialize with flock to prevent the
    # thundering-herd pattern where N concurrent monitoring sessions simultaneously
    # see an expired cache and each fires a separate GitHub API call.
    # After acquiring the lock, re-check the cache (double-checked locking):
    # a sibling session may have populated it while we waited.
    local lock_file="$GH_CACHE_DIR/${safe_key}.lock"
    local has_fallback=0
    [ $# -ge 4 ] && has_fallback=1
    if command -v flock &>/dev/null; then
        {
        flock -w "$GH_CACHE_LOCK_TIMEOUT" -x 200 2>/dev/null || true
        # Double-check after waiting for the lock: another session may have populated cache.
        # If the lock timed out, the fetch below proceeds without blocking forever.
        if [ -f "$cache_file" ]; then
            local mtime2
            mtime2=$(stat -c %Y "$cache_file" 2>/dev/null || stat -f %m "$cache_file" 2>/dev/null || echo "")
            if [ -n "$mtime2" ]; then
                local age2=$(( $(date +%s) - mtime2 ))
                if [ "$age2" -lt "$ttl" ]; then
                    cat "$cache_file"
                    return 0
                fi
            fi
        fi
        gh_cache_fetch_and_store "$producer" "$cache_file" "$safe_key" "$has_fallback" "$fallback"
    } 200>"$lock_file"
    else
        # flock not available (macOS, Alpine, minimal containers): direct fetch
        gh_cache_fetch_and_store "$producer" "$cache_file" "$safe_key" "$has_fallback" "$fallback"
    fi
}

# Fetch all PR data once per repo, with all fields needed by every check
# function. Cache state-tracked checks, but let merge-sensitive callers opt out
# so they always see live merge status.
fetch_pr_data_with_ttl() {
    local repo=$1
    local ttl=$2
    # Filter out draft PRs — they're intentionally deprioritized/not on merge path
    gh_cache_get_or_fetch "pr-${repo}" "$ttl" \
        "gh pr list --repo '$repo' --author '$AUTHOR' --state open \
            --json number,title,updatedAt,comments,latestReviews,statusCheckRollup,mergeable,mergeStateStatus,headRefOid,isDraft \
            --jq '[.[] | select(.isDraft | not)]' \
            2>/dev/null" \
        "[]"
}

# Cached view for state-tracked checks; default TTL is tuned to the 2-minute gate
# cadence to cut GraphQL load roughly in half.
fetch_pr_data() {
    fetch_pr_data_with_ttl "$1" "$GH_CACHE_TTL_PR"
}

# Live view for merge-sensitive checks. Uses GH_CACHE_TTL_LIVE_PR (default 180s)
# to cache merge-status fetches across consecutive 2-minute monitoring cycles.
# Set GH_CACHE_TTL_LIVE_PR=0 to restore uncached behaviour for debugging.
fetch_live_pr_data() {
    fetch_pr_data_with_ttl "$1" "$GH_CACHE_TTL_LIVE_PR"
}

# Check whether the last activity on a PR was from someone worth responding to.
# Returns 0 (true) if actionable, 1 if it should be skipped.
# Skips only two cases:
#   1. AUTHOR's own activity (self-triggered update)
#   2. Comments that @-mention someone else but NOT the AUTHOR
#      (e.g., "@greptileai review" — human talking to a bot, not to us)
# Bot reviews (Greptile, Codecov, etc.) are NOT filtered — they contain
# actionable feedback the agent should respond to.
#
# Checks both issue-style comments (.comments) and inline review comments
# (.latestReviews) since either can bump updatedAt.
has_actionable_update() {
    local pr_data=$1
    # pr_data is a JSON object with comments and latestReviews already included

    # Determine the most recent actor across both comments and reviews.
    # .comments are issue-style comments (chronological).
    # .latestReviews are the latest review per user (with submittedAt).
    # We use jq to find whichever is most recent by timestamp.
    local last_actor last_body
    last_actor=$(echo "$pr_data" | jq -r '
        [
            (.comments[-1] | select(. != null) | {login: .author.login, time: .createdAt, body: .body}),
            (.latestReviews | sort_by(.submittedAt) | last | select(. != null) | {login: .author.login, time: .submittedAt, body: .body})
        ]
        | sort_by(.time) | last | .login // empty
    ' 2>/dev/null)
    last_body=$(echo "$pr_data" | jq -r '
        [
            (.comments[-1] | select(. != null) | {time: .createdAt, body: .body}),
            (.latestReviews | sort_by(.submittedAt) | last | select(. != null) | {time: .submittedAt, body: .body})
        ]
        | sort_by(.time) | last | .body // empty
    ' 2>/dev/null)

    # If no activity found, this might be a push — allow it
    [ -z "$last_actor" ] && return 0

    # Skip if the last actor is the author (self-triggered update)
    if [ "$last_actor" = "$AUTHOR" ]; then
        return 1
    fi

    # Skip if the comment/review @-mentions someone else but NOT the AUTHOR.
    # This catches cases like "@greptileai review" or "@someone what do you think?"
    # where the commenter is clearly addressing someone other than the agent.
    if [ -n "$last_body" ]; then
        local mentions_anyone mentions_author
        # Match GitHub-style @mentions (start of line or after whitespace).
        # Avoids false positives from emails like user@example.com.
        mentions_anyone=$(echo "$last_body" | grep -cP '(^|\s)@\w+' || true)
        # Case-insensitive: GitHub usernames are case-insensitive
        mentions_author=$(echo "$last_body" | grep -ci "@${AUTHOR}" || true)
        if [ "${mentions_anyone:-0}" -gt 0 ] && [ "${mentions_author:-0}" -eq 0 ]; then
            # Mentions others but not us — not directed at AUTHOR
            return 1
        fi
    fi

    return 0
}

# Check for PR updates since last check (state-tracked via updatedAt timestamps)
# Filters out self-triggered updates and comments directed at others.
# Accepts pre-fetched PR data from fetch_pr_data() to avoid redundant API calls.
check_pr_updates() {
    local repo=$1
    local prs=$2
    [ "$prs" = "[]" ] || [ -z "$prs" ] && return 0

    echo "$prs" | jq -c '.[]' | while read -r pr_data; do
        local pr_number updated_at state_file
        pr_number=$(echo "$pr_data" | jq -r '.number')
        updated_at=$(echo "$pr_data" | jq -r '.updatedAt')
        state_file="$STATE_DIR/${repo//\//-}-pr-${pr_number}.state"

        if [ -f "$state_file" ]; then
            local last_check
            last_check=$(cat "$state_file")
            if [[ "$updated_at" > "$last_check" ]]; then
                # Check if the update was from someone we should respond to
                if has_actionable_update "$pr_data"; then
                    local pr_title
                    pr_title=$(echo "$pr_data" | jq -r '.title')
                    emit_item "pr_update" "$repo" "$pr_number" "$pr_title" "updated: $updated_at"
                fi
                # Always advance state to avoid rechecking this timestamp
                echo "$updated_at" > "$state_file"
            fi
        else
            # First time seeing this PR — seed state, don't report
            echo "$updated_at" > "$state_file"
        fi
    done
}

# Check for CI failures on open PRs (state-tracked — only triggers on CI state change)
# Tracks a hash of check conclusions so the same persistent failure doesn't re-trigger.
# Accepts pre-fetched PR data from fetch_pr_data() to avoid redundant API calls.
check_ci_failures() {
    local repo=$1
    local prs=$2
    [ "$prs" = "[]" ] || [ -z "$prs" ] && return 0

    echo "$prs" | jq -c '.[]' | while read -r pr_data; do
        local has_failures
        has_failures=$(echo "$pr_data" | jq -r 'select(.statusCheckRollup != null) | .statusCheckRollup | any(.conclusion == "FAILURE")')
        if [ "$has_failures" = "true" ]; then
            local pr_number pr_title ci_hash state_file
            pr_number=$(echo "$pr_data" | jq -r '.number')
            pr_title=$(echo "$pr_data" | jq -r '.title')
            # Hash the CI conclusions to detect state changes.
            # Note: in-progress checks have empty conclusions (mapped to "pending").
            # When they complete, the hash changes — this can cause a re-trigger even
            # if the final result matches the previous run. Acceptable trade-off vs
            # missing genuine state changes.
            ci_hash=$(echo "$pr_data" | jq -r '[.statusCheckRollup[] | .conclusion // "pending"] | sort | join(",")' | portable_hash)
            state_file="$STATE_DIR/${repo//\//-}-pr-${pr_number}-ci.state"

            if [ -f "$state_file" ]; then
                local last_hash
                last_hash=$(cat "$state_file")
                if [ "$ci_hash" = "$last_hash" ]; then
                    # CI state unchanged since last check — don't re-trigger
                    continue
                fi
            fi
            # New failure or CI state changed
            echo "$ci_hash" > "$state_file"
            emit_item "ci_failure" "$repo" "$pr_number" "$pr_title" "CI failing"
        fi
    done
}

# Check for assigned issues with new activity (state-tracked like PRs).
# Cached at GH_CACHE_TTL_ISSUE (default 300s); assigned-issue updates aren't
# 2-min-urgent — the gate's state-tracking still fires when cache refreshes.
check_assigned_issues() {
    local repo=$1
    local issues
    issues=$(gh_cache_get_or_fetch "issue-${repo}" "$GH_CACHE_TTL_ISSUE" \
        "gh issue list --repo '$repo' --assignee '$AUTHOR' --state open \
            --json number,title,updatedAt 2>/dev/null" \
        "[]")
    [ "$issues" = "[]" ] || [ -z "$issues" ] && return 0

    # Re-nag cooldown: when an issue is still awaiting our reply but its activity
    # hasn't advanced, re-emit at most this often so a dropped monitoring session
    # gets another chance without flooding every run.
    local now cooldown_seconds
    now=$(date +%s)
    cooldown_seconds=3600

    echo "$issues" | jq -c '.[]' | while read -r issue_data; do
        local issue_number updated_at issue_title state_file last_actor
        issue_number=$(echo "$issue_data" | jq -r '.number')
        updated_at=$(echo "$issue_data" | jq -r '.updatedAt')
        issue_title=$(echo "$issue_data" | jq -r '.title')
        state_file="$STATE_DIR/${repo//\//-}-issue-${issue_number}.state"

        # Resolution is keyed on the LAST ACTOR, not a timestamp watermark. An
        # assigned issue is "handled" only once AUTHOR is the last commenter.
        # This self-heals dropped sessions: a pending non-AUTHOR comment keeps
        # surfacing until it's actually answered, instead of going permanently
        # silent the moment the gate first emits (or silently seeding on first
        # sight when someone else already holds the ball). See alice#55.
        last_actor=$(gh api "repos/$repo/issues/$issue_number/comments" \
            --paginate --jq 'last.user.login // empty' 2>/dev/null | tail -1 || true)
        [ -z "$last_actor" ] && last_actor=$(gh api "repos/$repo/issues/$issue_number" \
            --jq '.user.login' 2>/dev/null || true)

        if [ "$last_actor" = "$AUTHOR" ]; then
            # Loop closed — record current activity so a future non-AUTHOR comment
            # re-triggers, and suppress emitting.
            echo "${updated_at}|${now}" > "$state_file"
            continue
        fi

        # Someone else is awaiting our reply. Emit on first sight or when activity
        # advances; otherwise re-nag once the cooldown elapses.
        if [ -f "$state_file" ]; then
            local last_updated last_emit
            IFS='|' read -r last_updated last_emit < "$state_file"
            if [ "$updated_at" = "$last_updated" ]; then
                local elapsed=$(( now - ${last_emit:-0} ))
                [ "$elapsed" -lt "$cooldown_seconds" ] && continue
            fi
        fi
        emit_item "assigned_issue" "$repo" "$issue_number" "$issue_title" "awaiting your reply (last: $last_actor)"
        echo "${updated_at}|${now}" > "$state_file"
    done
}

# Check for master/main branch CI failures (state-tracked by conclusion hash)
# These indicate regressions that slipped through — not tied to any specific PR.
check_master_ci() {
    local repo=$1
    local runs
    runs=$(gh_cache_get_or_fetch "run-master-${repo}" "$GH_CACHE_TTL_RUN" \
        "gh run list --repo '$repo' --branch master --limit 3 \
            --json databaseId,name,conclusion,createdAt 2>/dev/null" \
        "[]")
    # Also try 'main' if master returned nothing
    if [ "$runs" = "[]" ]; then
        runs=$(gh_cache_get_or_fetch "run-main-${repo}" "$GH_CACHE_TTL_RUN" \
            "gh run list --repo '$repo' --branch main --limit 3 \
                --json databaseId,name,conclusion,createdAt 2>/dev/null" \
            "[]")
    fi
    [ "$runs" = "[]" ] || [ -z "$runs" ] && return 0

    # Check for any recent failures
    local failures
    failures=$(echo "$runs" | jq -c '[.[] | select(.conclusion == "failure")]')
    local fail_count
    fail_count=$(echo "$failures" | jq 'length')
    [ "$fail_count" -eq 0 ] && return 0

    # State-track by hash of failure IDs to avoid re-triggering
    local ci_hash state_file
    ci_hash=$(echo "$failures" | jq -r '[.[].databaseId] | sort | join(",")' | portable_hash)
    state_file="$STATE_DIR/${repo//\//-}-master-ci.state"

    if [ -f "$state_file" ]; then
        local last_hash
        last_hash=$(cat "$state_file")
        [ "$ci_hash" = "$last_hash" ] && return 0
    fi
    echo "$ci_hash" > "$state_file"

    # Emit one item per failing run
    echo "$failures" | jq -c '.[]' | while read -r run; do
        local run_id run_name
        run_id=$(echo "$run" | jq -r '.databaseId')
        run_name=$(echo "$run" | jq -r '.name')
        emit_item "master_ci_failure" "$repo" "$run_id" "$run_name" "master branch CI failing"
    done
}

# Check for merge conflicts on open PRs (DIRTY or CONFLICTING status).
# Intentionally NOT state-tracked — conflicts should nag every run until resolved.
# Callers should pass live PR data (fetch_live_pr_data), not the cached view.
check_merge_conflicts() {
    local repo=$1
    local prs=$2
    [ "$prs" = "[]" ] || [ -z "$prs" ] && return 0

    echo "$prs" | jq -c '.[] | select(.mergeStateStatus == "DIRTY" or .mergeable == "CONFLICTING")' | while read -r pr_data; do
        local pr_number pr_title merge_status
        pr_number=$(echo "$pr_data" | jq -r '.number')
        pr_title=$(echo "$pr_data" | jq -r '.title')
        merge_status=$(echo "$pr_data" | jq -r '.mergeStateStatus')
        emit_item "merge_conflict" "$repo" "$pr_number" "$pr_title" "status: $merge_status"
    done
}

# Sweep open PRs for low Greptile review scores.
# Greptile updates review comments in-place, so updatedAt doesn't bump when the
# score changes. This function proactively finds PRs with low scores that need
# code fixes, even if no other activity has occurred.
#
# API cost: 1 REST call per open PR (issue comments endpoint), ONLY when the
# cached score is stale (> 60 min old) or a new HEAD SHA is detected. On quiet
# runs where no PRs have new commits, 0 API calls are made.
# HEAD SHA comes from fetch_pr_data() (headRefOid) — no extra API call needed.
#
# State tracking: $STATE_DIR/${repo_safe}-pr-${number}-greptile.state
#   Format: "score:timestamp:head_sha"
#   Re-emits when: (a) first time seeing a low score, or (b) new commits pushed
#   since last emission (fix attempt may have changed things).
#   Cooldown: 1 hour — won't re-emit the same PR within that window.
#   Score cache TTL: 60 min (= cooldown) — re-fetches after this window even if
#   HEAD unchanged (covers manual @greptileai triggers via greptile-helper.sh).
#
# Emits:
#   greptile_needs_fix       — score < 4 (significant findings to address)
#   greptile_needs_improvement — score = 4 (minor fixes needed)
check_greptile_scores() {
    local repo=$1
    local prs=$2
    [ "$prs" = "[]" ] || [ -z "$prs" ] && return 0

    local repo_safe="${repo//\//-}"
    local cooldown_seconds=3600  # 1 hour
    local fetch_cache_ttl=3600   # 60 min (= cooldown) — skip API call if score is cached for current HEAD SHA

    echo "$prs" | jq -c '.[]' | while read -r pr_data; do
        local pr_number pr_title
        pr_number=$(echo "$pr_data" | jq -r '.number')
        pr_title=$(echo "$pr_data" | jq -r '.title')

        # Extract HEAD SHA and read state file once — used for cache lookup,
        # cooldown check, and state updates throughout this iteration.
        local head_sha
        head_sha=$(echo "$pr_data" | jq -r '.headRefOid // "unknown"')
        local state_file="$STATE_DIR/${repo_safe}-pr-${pr_number}-greptile.state"
        local now
        now=$(date +%s)
        local last_state last_score last_timestamp last_sha
        last_state="" last_score="" last_timestamp=0 last_sha=""
        if [ -f "$state_file" ]; then
            last_state=$(cat "$state_file")
            last_score=$(echo "$last_state" | cut -d: -f1)
            last_timestamp=$(echo "$last_state" | cut -d: -f2)
            last_sha=$(echo "$last_state" | cut -d: -f3)
        fi

        # Skip the API call if we have a fresh cached score for the current HEAD SHA.
        # Greptile edits its review comment in-place (PR updatedAt doesn't bump), but
        # a re-review only occurs after new commits (→ head_sha changes, invalidates
        # cache) or a manual @greptileai trigger (greptile-helper.sh's 15-min guard
        # ensures any such review completes within the 30-min TTL).
        local greptile_score=""
        if [ -n "$last_state" ] && [ "$last_sha" = "$head_sha" ] \
                && [ $(( now - last_timestamp )) -lt "$fetch_cache_ttl" ]; then
            greptile_score="$last_score"
        else
            # Fetch issue comments and find Greptile review comment with a score.
            # Uses --paginate to handle PRs with >30 comments (default page size).
            # Greptile's bot username contains "greptile" (case-insensitive).
            # Look for "Score: N/5" pattern, anchored to avoid matching prose/flowcharts.
            #
            # Note: --paginate with --jq applies the filter per-page, so if multiple
            # pages each contain Greptile comments, we'd get multiple lines of output.
            # We take only the last line (tail -1) to get the most recent score.
            greptile_score=$(gh api "repos/${repo}/issues/${pr_number}/comments" \
                --paginate --jq '
                    [.[] | select(.user.login | test("greptile"; "i"))] | last |
                    .body // "" | capture("Score: (?<n>[0-9])/5") | .n // empty
                ' 2>/dev/null | tail -1 || true)
        fi

        # No Greptile review or no score found — skip
        # Guard against both empty string and literal "null" from jq
        [ -z "$greptile_score" ] || [ "$greptile_score" = "null" ] && continue

        # Guard: score must be a single decimal digit [0-9]. Non-digit values
        # (e.g. "}" from a failed jq capture whose tail -1 picks up the closing
        # brace of an empty object, or quoted strings like "5" when jq lacks -r)
        # bypass all numeric comparisons and silently fall through to
        # greptile_needs_improvement. Wipe the state file so the next sweep
        # re-fetches from the API instead of serving the corrupt cache forever.
        if ! [[ "$greptile_score" =~ ^[0-9]$ ]]; then
            rm -f "$state_file"
            continue
        fi

        # Score 5 = clean — update state file (so check_merge_ready sees
        # the perfect score instead of a stale sub-5 entry) and skip.
        if [ "$greptile_score" -ge 5 ] 2>/dev/null; then
            echo "${greptile_score}:${now}:${head_sha}" > "$state_file"
            continue
        fi

        # Score >= 4 is minor, < 4 needs fix
        local item_type
        if [ "$greptile_score" -lt 4 ]; then
            item_type="greptile_needs_fix"
        else
            item_type="greptile_needs_improvement"
        fi

        if [ -n "$last_state" ]; then
            # Same score and same HEAD — check cooldown
            if [ "$greptile_score" = "$last_score" ] && [ "$head_sha" = "$last_sha" ]; then
                local elapsed=$(( now - last_timestamp ))
                if [ "$elapsed" -lt "$cooldown_seconds" ]; then
                    # Within cooldown — skip
                    continue
                fi
                # Cooldown expired — re-emit to nag (score still low, no fix attempted)
            fi
            # Otherwise: score changed or new commits pushed — always re-emit
        else
            # First time seeing this PR — seed state, don't report
            echo "${greptile_score}:${now}:${head_sha}" > "$state_file"
            continue
        fi

        # Record state and emit
        echo "${greptile_score}:${now}:${head_sha}" > "$state_file"
        emit_item "$item_type" "$repo" "$pr_number" "$pr_title" "Greptile score: ${greptile_score}/5"
    done
}

# Surface own-PR Greptile review results that are invisible to the notification path.
#
# Bob opening his own PR and receiving a bot (Greptile/Codecov) review does NOT
# generate a review_requested/mention notification. check_greptile_scores seeds
# (and does NOT emit) on first discovery to avoid flooding when a repo is first
# onboarded — so a new PR's first Greptile result is silently recorded and only
# re-emitted after the 1-hour cooldown. This function closes that gap by emitting
# on first discovery of a changed signature for bot-authored PRs.
#
# Routing:
#   greptile < 4 (significant findings)   → greptile_needs_fix
#   greptile = 4 (minor improvements)     → greptile_needs_improvement
#   greptile >= 5                         → skip (perfect review is non-actionable here)
#   DIRTY / CONFLICTING                   → skip (check_merge_conflicts handles)
#   UNKNOWN                               → skip (GitHub still computing mergeability — transient)
#   BLOCKED                               → emit (stable "needs required review/checks" state;
#                                           a sub-5 Greptile score is actionable regardless of
#                                           merge-readiness — branch-protected green PRs sit here,
#                                           and skipping them left only the 1h-cooldown nag)
#
# State tracking: $STATE_DIR/${repo_safe}-pr-${number}-own-pr-review.state
#   Format: "${head_sha}:${greptile_score}:${merge_state}:${timestamp}"
#   Emit exactly once per (head_sha, greptile_score, merge_state) signature.
#   No timed cooldown re-emit — only re-emits when the signature changes.
#   Note: a BLOCKED→CLEAN transition changes merge_state, so a PR that emitted
#   at BLOCKED will emit again at CLEAN. This is intentional: the Greptile issue
#   is still unresolved after approval, and the duplicate is handled by the
#   downstream dispatcher's own dedup logic.
#
# API cost: zero — reads Greptile state files written by check_greptile_scores.
# Requires: live PR data (fresh mergeStateStatus/headRefOid).
check_own_pr_review_state() {
    local repo=$1
    local prs=$2  # live PR data (fetch_live_pr_data)
    [ "$prs" = "[]" ] || [ -z "$prs" ] && return 0

    local repo_safe="${repo//\//-}"

    echo "$prs" | jq -c '.[]' | while read -r pr_data; do
        local pr_number pr_title head_sha merge_state
        pr_number=$(echo "$pr_data" | jq -r '.number')
        pr_title=$(echo "$pr_data" | jq -r '.title')
        head_sha=$(echo "$pr_data" | jq -r '.headRefOid // "unknown"')
        merge_state=$(echo "$pr_data" | jq -r '.mergeStateStatus // "UNKNOWN"')

        # Skip conflict / genuinely-transient states — handled elsewhere or not yet settled.
        # BLOCKED is intentionally NOT skipped: it is the stable state for a branch-protected
        # green PR awaiting required review/approval, where a sub-5 Greptile score is still
        # actionable. UNKNOWN means GitHub has not finished computing mergeability (transient).
        case "$merge_state" in
            DIRTY|CONFLICTING) continue ;;
            UNKNOWN) continue ;;
        esac

        # Read Greptile score and reviewed SHA from state file written by check_greptile_scores
        # Format: score:timestamp:sha
        local greptile_state_file="$STATE_DIR/${repo_safe}-pr-${pr_number}-greptile.state"
        local greptile_score="" greptile_reviewed_sha=""
        if [ -f "$greptile_state_file" ]; then
            greptile_score=$(cut -d: -f1 < "$greptile_state_file")
            greptile_reviewed_sha=$(cut -d: -f3 < "$greptile_state_file")
        fi

        # No Greptile review on file yet — skip
        [ -z "$greptile_score" ] || [ "$greptile_score" = "null" ] && continue

        # Skip if the cached score is for a different SHA — Greptile hasn't reviewed
        # the current HEAD yet. Without this guard, a push within the 8-min pr_data
        # cache window produces a spurious dispatch pairing the new SHA with a stale score.
        if [ -n "$greptile_reviewed_sha" ] && [ "$greptile_reviewed_sha" != "$head_sha" ]; then
            continue
        fi

        # Greptile 5/5 is already a perfect review. Whether the PR is ready to
        # merge is handled separately by check_merge_ready / merge-status checks.
        if [ "$greptile_score" -ge 5 ] 2>/dev/null; then
            continue
        fi

        # Determine item type from Greptile score
        local item_type
        if [ "$greptile_score" -lt 4 ] 2>/dev/null; then
            item_type="greptile_needs_fix"
        else
            item_type="greptile_needs_improvement"
        fi

        # Emit once per (head_sha, greptile_score, merge_state) signature.
        # The last colon-delimited field is the timestamp; strip it for comparison.
        local state_file="$STATE_DIR/${repo_safe}-pr-${pr_number}-own-pr-review.state"
        local signature="${head_sha}:${greptile_score}:${merge_state}"
        local now
        now=$(date +%s)

        if [ -f "$state_file" ]; then
            local last_state last_sig
            last_state=$(cat "$state_file")
            last_sig="${last_state%:*}"  # drop trailing :timestamp
            [ "$last_sig" = "$signature" ] && continue  # same state — already dispatched
        fi

        # New or changed signature — record and emit
        echo "${signature}:${now}" > "$state_file"
        emit_item "$item_type" "$repo" "$pr_number" "$pr_title" \
            "own-PR review: Greptile ${greptile_score}/5 (merge_state: ${merge_state})"
    done
}

# Check if the bot account has already posted a maintainer-facing status comment
# on this PR indicating that the ball is in the maintainer's court.
#
# Signals the bot acknowledged it lacks merge permission on the target repo, so
# re-emitting merge_ready produces fake-ready churn (the same PR reappears every
# cooldown window despite nothing being actionable).
#
# We check the full comment history (not just the last comment) because later
# "CI is now green" status updates often overwrite the canonical waiting phrase,
# but the acknowledgment is still valid — subsequent re-emits would still
# produce the same "nothing changed" churn.
#
# Bot username is resolved from $BOT_USERNAME (default: TimeToBuildBob) to keep
# the helper reusable across forks.
#
# Returns 0 when the maintainer-waiting signal is present (i.e. SUPPRESS),
# 1 otherwise (emit as normal).
has_maintainer_waiting_comment() {
    local repo=$1
    local number=$2
    local bot="${BOT_USERNAME:-TimeToBuildBob}"

    local bot_comments
    bot_comments=$(gh api "repos/$repo/issues/$number/comments?per_page=100" \
        --jq "[.[] | select(.user.login == \"$bot\") | .body] | join(\"\n\")" 2>/dev/null) || return 1

    [ -n "$bot_comments" ] || return 1

    # Match any of the canonical or real-world phrasings that signal "waiting
    # only on a maintainer click." Matching is case-insensitive on the anchor
    # word so minor capitalisation drift doesn't defeat the guard.
    local lower
    lower=$(printf '%s' "$bot_comments" | tr '[:upper:]' '[:lower:]')
    case "$lower" in
        *"waiting only on a maintainer click"*) return 0 ;;
        *"waiting only on a maintainer merge click"*) return 0 ;;
        *"ready to merge when convenient"*) return 0 ;;
        *"blocked by missing mergepullrequest permission"*) return 0 ;;
    esac
    # "ready (to|for) merge @<maintainer>" — the @-mention indicates the ball
    # is explicitly in the maintainer's court. Bare "ready to merge" is too
    # broad (Bob says it about his own PRs in unrelated contexts), so we
    # require the @-mention as the maintainer-handoff signal.
    if printf '%s' "$lower" | grep -qE 'ready (to|for) merge @[a-z0-9_-]+'; then
        return 0
    fi
    return 1
}

# Find PRs that are ready to merge: CI green, no conflicts, and Greptile score
# is acceptable (>= 5/5, or no Greptile review at all for simple PRs).
#
# Unlike most checks, first-time discovery DOES emit — a merge-ready PR should
# be acted on immediately rather than silently seeded.
#
# State tracking: $STATE_DIR/${repo_safe}-pr-${number}-merge-ready.state
#   Cooldown: 12 hours — merge decisions shouldn't be nagged frequently.
#   Re-emits when HEAD SHA changes (new commits may change merge readiness).
#
# Suppression: If the bot already left a "waiting only on a maintainer click"
# status comment (see has_maintainer_waiting_comment), we skip emitting for
# that HEAD even after the cooldown expires. The state file is still bumped so
# subsequent runs follow the normal cooldown path once a new HEAD arrives.
#
# API cost: +1 comments fetch per CLEAN/MERGEABLE candidate that passes the
# Greptile and cooldown gates. Typical workload is a handful of PRs per cycle,
# so the added cost is negligible compared to the existing PR search calls.
check_merge_ready() {
    local repo=$1
    local prs=$2
    [ "$prs" = "[]" ] || [ -z "$prs" ] && return 0

    local repo_safe="${repo//\//-}"
    local cooldown_seconds=43200  # 12 hours

    # Filter to PRs with CLEAN merge state and MERGEABLE status
    echo "$prs" | jq -c '.[] | select(.mergeStateStatus == "CLEAN" and .mergeable == "MERGEABLE")' | while read -r pr_data; do
        local pr_number pr_title head_sha
        pr_number=$(echo "$pr_data" | jq -r '.number')
        pr_title=$(echo "$pr_data" | jq -r '.title')
        head_sha=$(echo "$pr_data" | jq -r '.headRefOid // "unknown"')
        [ -z "$head_sha" ] && head_sha="unknown"

        # Check Greptile score from state file (written by check_greptile_scores).
        # No API call needed — reuse the score already fetched.
        # If no state file exists, there's no Greptile review — OK to merge.
        local greptile_state_file="$STATE_DIR/${repo_safe}-pr-${pr_number}-greptile.state"
        local greptile_score=""
        if [ -f "$greptile_state_file" ]; then
            greptile_score=$(cut -d: -f1 < "$greptile_state_file")
            # Must be perfect score (>= 5) to be merge-ready
            if [ -n "$greptile_score" ] && [ "$greptile_score" -lt 5 ] 2>/dev/null; then
                continue
            fi
        fi

        # State tracking with cooldown
        local state_file="$STATE_DIR/${repo_safe}-pr-${pr_number}-merge-ready.state"
        local now
        now=$(date +%s)

        if [ -f "$state_file" ]; then
            local last_state last_sha last_timestamp
            last_state=$(cat "$state_file")
            last_sha=$(echo "$last_state" | cut -d: -f1)
            last_timestamp=$(echo "$last_state" | cut -d: -f2)

            # Same HEAD — check cooldown
            if [ "$head_sha" = "$last_sha" ]; then
                local elapsed=$(( now - last_timestamp ))
                if [ "$elapsed" -lt "$cooldown_seconds" ]; then
                    continue
                fi
            fi
            # HEAD changed or cooldown expired — re-emit
        fi
        # First-time discovery OR state change — emit immediately (no seed-only behavior)

        # Suppress re-emits when the bot already signalled it is waiting only on
        # a maintainer merge click. The state file is still updated so we stay
        # on the normal cooldown schedule once the situation changes (new HEAD,
        # new review, CI change that forces the "waiting" comment to be
        # refreshed into a different status).
        if has_maintainer_waiting_comment "$repo" "$pr_number"; then
            echo "${head_sha}:${now}" > "$state_file"
            continue
        fi

        echo "${head_sha}:${now}" > "$state_file"

        local detail="CI green, mergeable"
        if [ -n "$greptile_score" ] && [ "$greptile_score" != "null" ]; then
            detail="CI green, mergeable, Greptile ${greptile_score}/5"
        fi
        emit_item "merge_ready" "$repo" "$pr_number" "$pr_title" "$detail"
    done
}

# Check for actionable unread notifications (review requests, mentions, assigns, author, comments)
# State-tracked by notification ID to avoid re-triggering for the same unread notification.
# Returns individual notification items in jsonl mode, count in markdown mode.
check_notifications() {
    local notifs
    notifs=$(gh api notifications \
        --jq '.[] | select(.reason == "review_requested" or .reason == "mention" or .reason == "assign" or .reason == "author" or .reason == "comment")' \
        2>/dev/null) || return 0
    [ -z "$notifs" ] && return 0

    # Cap emitted notifications per run to avoid flooding the dispatcher when
    # a filter change (e.g. adding new reasons) unlocks a large backlog.
    # First-seen notifications are always seeded (state written, not emitted).
    # The cap only gates emit-eligible items (strictly-newer updated_at): those
    # skipped by the cap get neither emitted nor persisted, so they retry next run.
    local max_notif_per_run=5

    # Notification state files store the most recently seen `updated_at`. GitHub
    # re-uses the same notification ID across follow-up comments on the same
    # thread (only `updated_at` advances), so a presence-only check would dedupe
    # legitimate follow-up activity. Re-emit when `updated_at` is strictly newer
    # than the stored timestamp.
    if [ "$FORMAT" = "jsonl" ]; then
        local _notif_emitted=0
        echo "$notifs" | jq -c '{
            id: .id,
            updated_at: .updated_at,
            type: "notification",
            repo: .repository.full_name,
            number: (.subject.url // "" | split("/") | last | tonumber? // 0),
            title: .subject.title,
            detail: .reason
        }' 2>/dev/null | while IFS= read -r item; do
            local notif_id notif_updated state_file prior
            notif_id=$(echo "$item" | jq -r '.id')
            notif_updated=$(echo "$item" | jq -r '.updated_at')
            state_file="$STATE_DIR/notif-${notif_id}.state"
            prior=""
            [ -f "$state_file" ] && prior=$(cat "$state_file" 2>/dev/null || true)
            # Seed-on-first-sight: when there's no prior state (first run, or after the
            # state dir is reset/cleaned), record the timestamp but do NOT emit. This
            # matches the documented contract ("On first run, all items are seeded but
            # NOT reported") and every other check here (check_pr_updates,
            # check_greptile_scores). Without it, a wiped state dir makes the whole
            # unread backlog look "new" and fires a noop session investigating already-
            # resolved threads. Only a strictly-newer updated_at (genuine follow-up
            # activity) emits. String comparison works on ISO-8601 timestamps.
            if [ -z "$prior" ]; then
                printf '%s' "$notif_updated" > "$state_file"
            elif [ "$prior" \< "$notif_updated" ]; then
                _notif_emitted=$((_notif_emitted + 1))
                if [ "$_notif_emitted" -le "$max_notif_per_run" ]; then
                    # Emit first so a jq failure leaves the state file untouched and the
                    # notification is retried on the next run (emit-before-persist semantics).
                    echo "$item" | jq -c 'del(.id, .updated_at)'
                    printf '%s' "$notif_updated" > "$state_file"
                fi
            fi
        done
    else
        # Count new notifications and create state files (process substitution avoids subshell)
        local new_count=0
        while IFS= read -r line; do
            local notif_id notif_updated state_file prior
            notif_id=${line%%$'\t'*}
            notif_updated=${line#*$'\t'}
            state_file="$STATE_DIR/notif-${notif_id}.state"
            prior=""
            [ -f "$state_file" ] && prior=$(cat "$state_file" 2>/dev/null || true)
            # Seed-on-first-sight (see jsonl branch above for rationale): a first-seen
            # notification is recorded but not counted, so a reset state dir doesn't
            # report the whole unread backlog as "new" and trigger a noop session.
            if [ -z "$prior" ]; then
                printf '%s' "$notif_updated" > "$state_file"
            elif [ "$prior" \< "$notif_updated" ]; then
                printf '%s' "$notif_updated" > "$state_file"
                new_count=$((new_count + 1))
            fi
        done < <(echo "$notifs" | jq -r '"\(.id)\t\(.updated_at)"' 2>/dev/null)
        echo "$new_count"
    fi
}

# --- Main ---

# Deduplicate repos (EXTRA_REPOS may overlap with org repos)
all_repos=$(discover_repos | sort -u)
all_items=""

# Process repos in parallel — each repo's checks are independent (state files
# are repo-prefixed, no cross-repo contention). Collect output via temp files.
# Cap concurrency at 8 to avoid GitHub API rate limits.
PARALLEL_TMPDIR=$(mktemp -d)
trap 'rm -rf "$PARALLEL_TMPDIR"' EXIT
MAX_PARALLEL=8
running=0

for repo in $all_repos; do
    repo_safe="${repo//\//-}"
    (
        # Cached PR data drives the state-tracked checks.
        pr_data=$(fetch_pr_data "$repo")
        # Merge-sensitive checks need live status every run, but only for repos
        # that actually have open author PRs. When the cached lane already shows
        # no open PRs, a live merge-status fetch can only return the same empty
        # set, so skip the GraphQL call. On a multi-repo org most repos have zero
        # open author PRs at any moment, so this removes the dominant idle-time
        # GraphQL burner without weakening live merge detection for repos that do
        # have PRs. See tasks/github-graphql-rate-limit-regression.md.
        if [ "$(printf '%s' "$pr_data" | jq 'length' 2>/dev/null || echo 0)" -gt 0 ]; then
            live_pr_data=$(fetch_live_pr_data "$repo")
        else
            live_pr_data="[]"
        fi
        repo_items=""

        items=$(check_pr_updates "$repo" "$pr_data" 2>/dev/null || true)
        [ -n "$items" ] && repo_items+="$items"$'\n'

        items=$(check_ci_failures "$repo" "$pr_data" 2>/dev/null || true)
        [ -n "$items" ] && repo_items+="$items"$'\n'

        items=$(check_assigned_issues "$repo" 2>/dev/null || true)
        [ -n "$items" ] && repo_items+="$items"$'\n'

        items=$(check_master_ci "$repo" 2>/dev/null || true)
        [ -n "$items" ] && repo_items+="$items"$'\n'

        items=$(check_merge_conflicts "$repo" "$live_pr_data" 2>/dev/null || true)
        [ -n "$items" ] && repo_items+="$items"$'\n'

        items=$(check_greptile_scores "$repo" "$pr_data" 2>/dev/null || true)
        [ -n "$items" ] && repo_items+="$items"$'\n'

        # Must run AFTER check_greptile_scores (reads its state files) and with
        # live_pr_data (needs fresh mergeStateStatus for routing decisions).
        items=$(check_own_pr_review_state "$repo" "$live_pr_data" 2>/dev/null || true)
        [ -n "$items" ] && repo_items+="$items"$'\n'

        items=$(check_merge_ready "$repo" "$live_pr_data" 2>/dev/null || true)
        [ -n "$items" ] && repo_items+="$items"$'\n'

        [ -n "$repo_items" ] && printf '%s' "$repo_items" > "$PARALLEL_TMPDIR/$repo_safe"
    ) &
    running=$((running + 1))
    if [ "$running" -ge "$MAX_PARALLEL" ]; then
        # wait -n (bash 4.3+) waits for any single child; fall back to wait-all
        if wait -n 2>/dev/null; then
            running=$((running - 1))
        else
            # wait -n not available (bash <4.3): wait for all, reset counter
            wait
            running=0
        fi
    fi
done
wait

# Collect results from all repos
shopt -s nullglob
for f in "$PARALLEL_TMPDIR"/*; do
    all_items+="$(cat "$f")"$'\n'
done
shopt -u nullglob

if [ "$FORMAT" = "jsonl" ]; then
    notif_items=$(check_notifications)
    [ -n "$notif_items" ] && all_items+="$notif_items"$'\n'
else
    notif_count=$(check_notifications)
    if [ "$notif_count" -gt 0 ] 2>/dev/null; then
        all_items+="notifications — $notif_count actionable (review requests, mentions, assigns, author, comments)"$'\n'
    fi
fi

# Trim trailing newlines and check if we found anything
all_items=$(echo "$all_items" | sed '/^$/d')

if [ -n "$all_items" ]; then
    if [ "$FORMAT" = "markdown" ]; then
        # Group by repo for readable output
        # (items already have repo context in emit_item output)
        echo "$all_items"
    else
        # jsonl: one JSON object per line, ready for iteration
        echo "$all_items"
    fi
    exit 0
else
    exit 1
fi
