#!/bin/bash
# Multi-repository CI status checker
# Shows status of GitHub Actions workflows across multiple repositories

set -euo pipefail

# gh colorizes --json/--jq output whenever it thinks stdout is a TTY, which
# breaks downstream jq. GH_FORCE_TTY is NOT a boolean: gh treats ANY set value
# (including "0") as "force TTY output" (optionally parsed as display width),
# so exporting GH_FORCE_TTY=0 (#1266) caused the exact corruption it meant to
# prevent. Unset it, and set NO_COLOR so a real PTY (context pipelines capture
# through one) cannot re-enable colorized JSON either.
unset GH_FORCE_TTY
export NO_COLOR=1

# Get GitHub user (from auth or env var)
GH_USER="${GH_USER:-$(gh api user -q .login 2>/dev/null || echo "")}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# --- default_branch cache ---
# `api repos/<REPO> .default_branch` was the single largest REST consumer in this
# script (512 calls/window, ~38% of its traffic) even though a repo's default
# branch is effectively immutable — it changes only via a rare manual rename.
# Cache it to disk with a 6-hour TTL (self-heals if a branch rename occurs;
# clear immediately with `rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/repo-status"`)
# so it is fetched once and reused across every subsequent run and session.
# 6h (not 7d): if a branch is renamed but the old branch kept alive, gh run list
# succeeds on the stale name (no error → no self-heal). Shorter TTL bounds the
# stale window while still eliminating the vast majority of repeated API calls.
_DB_CACHE_DIR="${BOB_DEFAULT_BRANCH_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/repo-status/default-branch}"
_DB_CACHE_TTL=21600  # 6 hours — reduces stale window for rename-with-old-branch-kept scenarios

_default_branch() {
    local repo="$1"
    local cache_file now mtime age
    cache_file="$_DB_CACHE_DIR/${repo//\//__}"
    if [ -f "$cache_file" ]; then
        now=$(date +%s)
        mtime=$(stat -c %Y "$cache_file" 2>/dev/null || stat -f %m "$cache_file" 2>/dev/null || printf '0')
        age=$(( now - mtime ))
        if [ "$age" -lt "$_DB_CACHE_TTL" ]; then
            cat "$cache_file"
            return 0
        fi
    fi
    local db
    db=$(gh api "repos/$repo" --jq '.default_branch' 2>/dev/null || true)
    if [ -n "$db" ]; then
        mkdir -p "$_DB_CACHE_DIR" 2>/dev/null || true
        # Atomic write so a concurrent check_repo can never read a torn file.
        printf '%s' "$db" > "$cache_file.tmp.$$" 2>/dev/null \
            && mv "$cache_file.tmp.$$" "$cache_file" 2>/dev/null \
            || rm -f "$cache_file.tmp.$$" 2>/dev/null
        printf '%s' "$db"
    else
        printf 'master'
    fi
}

# The disabled-workflow set (`gh workflow list --all`) was ~540 calls/window —
# comparable to default_branch. Unlike default_branch it *can* change (an
# operator disables a flaky workflow), so it gets a 1h TTL rather than a
# cache-forever: worst case is a stale report for a freshly-disabled workflow,
# which self-heals on the next hour's refetch.
_WF_CACHE_DIR="${BOB_DISABLED_WORKFLOW_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/repo-status/disabled-workflows}"

_disabled_workflows() {
    local repo="$1"
    local cache_file now mtime age wf
    cache_file="$_WF_CACHE_DIR/${repo//\//__}"
    if [ -f "$cache_file" ]; then
        now=$(date +%s)
        mtime=$(stat -c %Y "$cache_file" 2>/dev/null || stat -f %m "$cache_file" 2>/dev/null || printf '0')
        age=$(( now - mtime ))
        if [ "$age" -lt 3600 ]; then
            cat "$cache_file"
            return 0
        fi
    fi
    wf=$(gh workflow list --repo "$repo" --all --json name,state --jq '[.[] | select(.state == "disabled_manually") | .name]' 2>/dev/null || true)
    # Only cache when the API returned a valid non-empty JSON array.
    # An empty/missing response (transient error, rate limit) must not be cached
    # as '[]' for an hour — that would hide disabled workflows until TTL expires.
    if [ -n "$wf" ]; then
        mkdir -p "$_WF_CACHE_DIR" 2>/dev/null || true
        printf '%s' "$wf" > "$cache_file.tmp.$$" 2>/dev/null \
            && mv "$cache_file.tmp.$$" "$cache_file" 2>/dev/null \
            || rm -f "$cache_file.tmp.$$" 2>/dev/null
    else
        wf='[]'
    fi
    printf '%s' "$wf"
}

# Current HEAD SHA (`gh api repos/<REPO>/commits .[0].sha`) was ~151 calls/window.
# Unlike default_branch it *does* change (on every commit push), but it changes
# infrequently enough that a 5-minute TTL prevents redundant fetches during a
# session without introducing problematic staleness. If the script runs multiple
# times within a stale-run's 5-minute window, HEAD detection still works; if it
# misses a very recent push, it will report stale for one run, then auto-correct
# on the next refresh cycle. This is acceptable for stale-run *annotation*, not
# control flow.
_HEAD_SHA_CACHE_DIR="${BOB_HEAD_SHA_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/repo-status/head-sha}"
_HEAD_SHA_CACHE_TTL=300  # 5 minutes

_current_head_sha() {
    local repo="$1"
    local cache_file now mtime age sha
    cache_file="$_HEAD_SHA_CACHE_DIR/${repo//\//__}"
    if [ -f "$cache_file" ]; then
        now=$(date +%s)
        mtime=$(stat -c %Y "$cache_file" 2>/dev/null || stat -f %m "$cache_file" 2>/dev/null || printf '0')
        age=$(( now - mtime ))
        if [ "$age" -lt "$_HEAD_SHA_CACHE_TTL" ]; then
            cat "$cache_file"
            return 0
        fi
    fi
    # Use `// empty` so jq emits nothing (not the string "null") for repos with no commits.
    sha=$(gh api "repos/$repo/commits" --jq '.[0].sha // empty' 2>/dev/null || echo "")
    if [ -n "$sha" ]; then
        mkdir -p "$_HEAD_SHA_CACHE_DIR" 2>/dev/null || true
        # Atomic write so concurrent checks never read a torn file.
        printf '%s' "$sha" > "$cache_file.tmp.$$" 2>/dev/null \
            && mv "$cache_file.tmp.$$" "$cache_file" 2>/dev/null \
            || rm -f "$cache_file.tmp.$$" 2>/dev/null
    fi
    printf '%s' "$sha"
}

check_repo() {
    local repo=$1
    local label=${2:-$repo}

    # Scope all run queries to the default branch so feature-branch runs do not
    # appear in the list and trigger false-positive stale annotations (a
    # feature-branch headSha is always different from the default-branch HEAD).
    local default_branch
    default_branch=$(_default_branch "$repo")

    # Fetch last 5 runs on the default branch so we can skip disabled-workflow
    # runs and still have a fallback.  headSha is needed so we can detect when
    # the most recent run lives on an older commit than the current
    # default-branch HEAD — a common case when path filters skip CI on
    # journal-only / docs-only commits.
    local run_json run_err_file run_err
    run_err_file=$(mktemp)
    run_json=$(gh run list --repo "$repo" --branch "$default_branch" --limit 5 --json conclusion,status,url,name,headSha 2>"$run_err_file" || echo "error")
    run_err=$(cat "$run_err_file"); rm -f "$run_err_file"

    if [ "$run_json" = "error" ]; then
        # A cached default branch can go stale if the repo renamed it (7-day TTL) —
        # `gh run list --branch <stale-name>` errors on a branch that no longer
        # exists. Drop the cache and retry once with a fresh fetch so a rename
        # self-heals immediately instead of showing "No Actions" for up to 7 days.
        # Only invalidate the cache on branch-not-found errors; transient failures
        # (network, rate-limit) should not discard a possibly-correct cached branch.
        if echo "$run_err" | grep -qiE "not found|no commit|does not exist|unknown ref|no ref|could not resolve|no such branch"; then
            rm -f "$_DB_CACHE_DIR/${repo//\//__}" 2>/dev/null || true
            default_branch=$(_default_branch "$repo")
            run_json=$(gh run list --repo "$repo" --branch "$default_branch" --limit 5 --json conclusion,status,url,name,headSha 2>/dev/null || echo "error")
        fi
    fi

    if [ "$run_json" = "error" ]; then
        echo -e "${YELLOW}-${NC} $label: No Actions"
        return
    fi

    if [ "$run_json" = "[]" ]; then
        echo -e "${YELLOW}-${NC} $label: No runs"
        return
    fi

    # Filter out runs from manually disabled workflows (e.g. stale fork workflows)
    local disabled_json
    disabled_json=$(_disabled_workflows "$repo")
    if [ "$disabled_json" != "[]" ]; then
        run_json=$(echo "$run_json" | jq --argjson disabled "$disabled_json" '[.[] | select(.name as $n | $disabled | index($n) | not)]')
    fi

    # Filter out runs with "skipped" conclusion — conditional workflows that don't apply
    # to the current event type (e.g. gptme-bot only runs on PR/issue events, gets
    # "skipped" on master pushes and otherwise masks the passing build/test runs).
    # Only filter if there are non-skipped runs to fall back to.
    local non_skipped_json
    non_skipped_json=$(echo "$run_json" | jq '[.[] | select(.conclusion != "skipped")]')
    if [ "$(echo "$non_skipped_json" | jq 'length')" -gt 0 ]; then
        run_json="$non_skipped_json"
    fi

    local conclusion status in_progress=""
    conclusion=$(echo "$run_json" | jq -r '.[0].conclusion // ""')
    status=$(echo "$run_json" | jq -r '.[0].status // ""')

    # If latest run is in-progress, use the previous run's conclusion instead
    if [ -z "$conclusion" ] && [[ "$status" =~ ^(in_progress|queued|waiting|pending|requested)$ ]]; then
        in_progress=1
        conclusion=$(echo "$run_json" | jq -r '.[1].conclusion // ""' 2>/dev/null)
    fi

    local suffix=""
    [ -n "$in_progress" ] && suffix=" (run in progress)"

    # Determine index of the run we're reporting on (1 if latest is in-progress, else 0)
    local idx=0
    [ -n "$in_progress" ] && idx=1

    # Stale-SHA detection: if the reported run was on a commit that is no longer HEAD
    # (e.g. because path filters skipped CI on newer commits), annotate the output so
    # we don't treat a stale red/green as authoritative for HEAD.
    # Skipped entirely when latest run is in-progress — caller already signaled that
    # fresh CI is running, so "stale" would be noise.
    local stale_suffix=""
    if [ -z "$in_progress" ]; then
        local run_head_sha
        run_head_sha=$(echo "$run_json" | jq -r ".[$idx].headSha // \"\"")
        if [ -n "$run_head_sha" ]; then
            local current_head_sha
            current_head_sha=$(_current_head_sha "$repo")
            if [ -n "$current_head_sha" ] && [ "$run_head_sha" != "$current_head_sha" ]; then
                stale_suffix=" (stale; HEAD=${current_head_sha:0:7}, run=${run_head_sha:0:7})"
            fi
        fi
    fi

    case "$conclusion" in
        "success")
            echo -e "${GREEN}✓${NC} $label: Passing${suffix}${stale_suffix}"
            ;;
        "failure")
            echo -e "${RED}✗${NC} $label: Failing${suffix}${stale_suffix}"
            # Show URL for the failing run
            local workflow_url
            workflow_url=$(echo "$run_json" | jq -r ".[$idx].url // \"\"")
            if [ -n "$workflow_url" ]; then
                echo "  $workflow_url"
            fi
            ;;
        "cancelled")
            echo -e "${YELLOW}⚠${NC} $label: Cancelled${suffix}${stale_suffix}"
            ;;
        "skipped")
            echo -e "${YELLOW}⊘${NC} $label: Skipped${suffix}${stale_suffix}"
            ;;
        "")
            # No previous run to fall back on
            if [ -n "$in_progress" ]; then
                echo -e "${YELLOW}⏳${NC} $label: In progress (no previous run)"
            else
                echo "? $label: Unknown ($status)"
            fi
            ;;
        *)
            echo "? $label: $conclusion${suffix}"
            ;;
    esac
}

echo "=== Repository CI Status ==="
echo

# If arguments provided, use them as repos
if [ $# -gt 0 ]; then
    # Process repos from arguments in parallel, collect output
    # Format: "owner/repo:label" or just "owner/repo" (label defaults to repo name)
    TMPDIR=$(mktemp -d)
    trap 'rm -rf "$TMPDIR"' EXIT

    i=0
    for arg in "$@"; do
        if [[ "$arg" == *":"* ]]; then
            repo="${arg%:*}"
            label="${arg#*:}"
            check_repo "$repo" "$label" > "$TMPDIR/$i.txt" 2>&1 &
        else
            check_repo "$arg" > "$TMPDIR/$i.txt" 2>&1 &
        fi
        i=$((i + 1))
    done
    wait  # Wait for all parallel checks to complete

    # Print results in order (iterate by index to handle 10+ repos correctly)
    for j in $(seq 0 $((i - 1))); do
        [ -f "$TMPDIR/$j.txt" ] && cat "$TMPDIR/$j.txt"
    done
else
    # Dynamically build repo list: gptme org (non-archived) + recently updated personal repos
    # Both calls are fast (~1s each) and run in parallel
    TMPDIR=$(mktemp -d)
    trap 'rm -rf "$TMPDIR"' EXIT

    # Fetch repo lists in parallel
    gh repo list gptme --no-archived --json nameWithOwner --jq '.[].nameWithOwner' --limit 30 > "$TMPDIR/org_repos.txt" 2>/dev/null &
    gh repo list "${GH_USER:-ErikBjare}" --no-archived --source --json nameWithOwner,pushedAt --limit 10 > "$TMPDIR/personal_repos.json" 2>/dev/null &
    wait

    # Get 5 most recently pushed personal repos
    python3 -c "
import json, sys
try:
    repos = json.load(open('$TMPDIR/personal_repos.json'))
    repos.sort(key=lambda r: r['pushedAt'], reverse=True)
    for r in repos[:5]:
        print(r['nameWithOwner'])
except Exception:
    pass
" > "$TMPDIR/personal_repos.txt" 2>/dev/null

    # Combine and deduplicate
    all_repos=$(cat "$TMPDIR/org_repos.txt" "$TMPDIR/personal_repos.txt" 2>/dev/null | sort -u)

    if [ -z "$all_repos" ]; then
        echo "Unable to fetch repo list"
        exit 1
    fi

    # Check all repos in parallel
    i=0
    while read -r repo; do
        [ -n "$repo" ] && check_repo "$repo" > "$TMPDIR/$i.txt" 2>&1 &
        i=$((i + 1))
    done <<< "$all_repos"
    wait

    # Print results in order (iterate by index to handle 10+ repos correctly)
    for j in $(seq 0 $((i - 1))); do
        [ -f "$TMPDIR/$j.txt" ] && cat "$TMPDIR/$j.txt"
    done
fi
