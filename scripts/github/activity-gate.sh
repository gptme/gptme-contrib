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
#   Greptile score sweep: Proactively finds PRs with low Greptile scores (< 5/5)
#   that need code fixes. Greptile updates comments in-place, so updatedAt never
#   bumps — without this sweep, low-scored PRs sit indefinitely. State-tracked
#   by score + HEAD SHA with 1-hour cooldown. Costs 1 extra REST API call per
#   open PR (issue comments endpoint). HEAD SHA comes from fetch_pr_data().
#
#   Notifications: Filters for actionable reasons (review_requested, mention,
#   assign, author, comment). State-tracked by notification ID. State files
#   accumulate in STATE_DIR/notif-*.state. GitHub notifications clear when
#   marked as read upstream, so old state files become inert.
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
#   API efficiency: PR data is fetched once per repo via fetch_pr_data() and
#   shared across check_pr_updates, check_ci_failures, check_merge_conflicts,
#   and check_merge_ready.
#   This reduces gh pr list calls from 3N to N (where N = number of repos).
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
    # Cache repo list for 1 hour — org membership rarely changes
    local cache_file="$STATE_DIR/repo-list-${ORG}.cache"
    local cache_max_age=3600  # seconds

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

# Fetch all PR data once per repo, with all fields needed by every check function.
# This replaces 3 separate `gh pr list` calls with a single one.
fetch_pr_data() {
    local repo=$1
    # Filter out draft PRs — they're intentionally deprioritized/not on merge path
    gh pr list --repo "$repo" --author "$AUTHOR" --state open \
        --json number,title,updatedAt,comments,latestReviews,statusCheckRollup,mergeable,mergeStateStatus,headRefOid,isDraft \
        --jq '[.[] | select(.isDraft | not)]' \
        2>/dev/null || echo "[]"
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

# Check for assigned issues with new activity (state-tracked like PRs)
check_assigned_issues() {
    local repo=$1
    local issues
    issues=$(gh issue list --repo "$repo" --assignee "$AUTHOR" --state open \
        --json number,title,updatedAt 2>/dev/null || echo "[]")
    [ "$issues" = "[]" ] || [ -z "$issues" ] && return 0

    echo "$issues" | jq -c '.[]' | while read -r issue_data; do
        local issue_number updated_at state_file
        issue_number=$(echo "$issue_data" | jq -r '.number')
        updated_at=$(echo "$issue_data" | jq -r '.updatedAt')
        state_file="$STATE_DIR/${repo//\//-}-issue-${issue_number}.state"

        if [ -f "$state_file" ]; then
            local last_check
            last_check=$(cat "$state_file")
            if [[ "$updated_at" > "$last_check" ]]; then
                local issue_title
                issue_title=$(echo "$issue_data" | jq -r '.title')
                emit_item "assigned_issue" "$repo" "$issue_number" "$issue_title" "updated: $updated_at"
                echo "$updated_at" > "$state_file"
            fi
        else
            # First time seeing this issue — seed state, don't report
            echo "$updated_at" > "$state_file"
        fi
    done
}

# Check for master/main branch CI failures (state-tracked by conclusion hash)
# These indicate regressions that slipped through — not tied to any specific PR.
check_master_ci() {
    local repo=$1
    local runs
    runs=$(gh run list --repo "$repo" --branch master --limit 3 \
        --json databaseId,name,conclusion,createdAt 2>/dev/null || echo "[]")
    # Also try 'main' if master returned nothing
    if [ "$runs" = "[]" ]; then
        runs=$(gh run list --repo "$repo" --branch main --limit 3 \
            --json databaseId,name,conclusion,createdAt 2>/dev/null || echo "[]")
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
# Accepts pre-fetched PR data from fetch_pr_data() to avoid redundant API calls.
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
    # Only emitted notifications get state files — unemitted ones retry next run.
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
            # Re-emit if no state file yet OR stored timestamp is older than current.
            # String comparison works on ISO-8601 timestamps.
            if [ -z "$prior" ] || [ "$prior" \< "$notif_updated" ]; then
                _notif_emitted=$((_notif_emitted + 1))
                if [ "$_notif_emitted" -le "$max_notif_per_run" ]; then
                    printf '%s' "$notif_updated" > "$state_file"
                    # Strip id and updated_at before emitting (consumer doesn't need them)
                    echo "$item" | jq -c 'del(.id, .updated_at)'
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
            if [ -z "$prior" ] || [ "$prior" \< "$notif_updated" ]; then
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
        # Fetch all PR data once per repo (replaces 3 separate gh pr list calls)
        pr_data=$(fetch_pr_data "$repo")
        repo_items=""

        items=$(check_pr_updates "$repo" "$pr_data" 2>/dev/null || true)
        [ -n "$items" ] && repo_items+="$items"$'\n'

        items=$(check_ci_failures "$repo" "$pr_data" 2>/dev/null || true)
        [ -n "$items" ] && repo_items+="$items"$'\n'

        items=$(check_assigned_issues "$repo" 2>/dev/null || true)
        [ -n "$items" ] && repo_items+="$items"$'\n'

        items=$(check_master_ci "$repo" 2>/dev/null || true)
        [ -n "$items" ] && repo_items+="$items"$'\n'

        items=$(check_merge_conflicts "$repo" "$pr_data" 2>/dev/null || true)
        [ -n "$items" ] && repo_items+="$items"$'\n'

        items=$(check_greptile_scores "$repo" "$pr_data" 2>/dev/null || true)
        [ -n "$items" ] && repo_items+="$items"$'\n'

        items=$(check_merge_ready "$repo" "$pr_data" 2>/dev/null || true)
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
