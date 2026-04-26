#!/bin/bash
# Multi-repository CI status checker
# Shows status of GitHub Actions workflows across multiple repositories

set -euo pipefail

# Get GitHub user (from auth or env var)
GH_USER="${GH_USER:-$(gh api user -q .login 2>/dev/null || echo "")}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

check_repo() {
    local repo=$1
    local label=${2:-$repo}

    # Scope all run queries to the default branch so feature-branch runs do not
    # appear in the list and trigger false-positive stale annotations (a
    # feature-branch headSha is always different from the default-branch HEAD).
    local default_branch
    default_branch=$(gh api "repos/$repo" --jq '.default_branch' 2>/dev/null || echo "master")

    # Fetch last 5 runs on the default branch so we can skip disabled-workflow
    # runs and still have a fallback.  headSha is needed so we can detect when
    # the most recent run lives on an older commit than the current
    # default-branch HEAD — a common case when path filters skip CI on
    # journal-only / docs-only commits.
    local run_json
    run_json=$(gh run list --repo "$repo" --branch "$default_branch" --limit 5 --json conclusion,status,url,name,headSha 2>/dev/null || echo "error")

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
    disabled_json=$(gh workflow list --repo "$repo" --all --json name,state --jq '[.[] | select(.state == "disabled_manually") | .name]' 2>/dev/null || echo "[]")
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
            current_head_sha=$(gh api "repos/$repo/commits" --jq '.[0].sha' 2>/dev/null || echo "")
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
        "cancelled"|"skipped")
            echo -e "${YELLOW}⚠${NC} $label: $conclusion${suffix}${stale_suffix}"
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
