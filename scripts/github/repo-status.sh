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

    # Fetch last 2 runs so we can show previous result if latest is in-progress
    local run_json
    run_json=$(gh run list --repo "$repo" --limit 2 --json conclusion,status,url 2>/dev/null || echo "error")

    if [ "$run_json" = "error" ]; then
        echo -e "${YELLOW}-${NC} $label: No Actions"
        return
    fi

    if [ "$run_json" = "[]" ]; then
        echo -e "${YELLOW}-${NC} $label: No runs"
        return
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

    case "$conclusion" in
        "success")
            echo -e "${GREEN}✓${NC} $label: Passing${suffix}"
            ;;
        "failure")
            echo -e "${RED}✗${NC} $label: Failing${suffix}"
            # Show URL for the failing run (index 1 if in-progress, else 0)
            local idx=0
            [ -n "$in_progress" ] && idx=1
            local workflow_url
            workflow_url=$(echo "$run_json" | jq -r ".[$idx].url // \"\"")
            if [ -n "$workflow_url" ]; then
                echo "  $workflow_url"
            fi
            ;;
        "cancelled"|"skipped")
            echo -e "${YELLOW}⚠${NC} $label: $conclusion${suffix}"
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
        ((i++))
    done
    wait  # Wait for all parallel checks to complete

    # Print results in order
    for f in "$TMPDIR"/*.txt; do
        cat "$f"
    done
else
    # Dynamically build repo list: gptme org (non-archived) + recently updated personal repos
    # Both calls are fast (~1s each) and run in parallel
    TMPDIR=$(mktemp -d)
    trap 'rm -rf "$TMPDIR"' EXIT

    # Fetch repo lists in parallel
    gh repo list gptme --no-archived --json nameWithOwner --jq '.[].nameWithOwner' --limit 30 > "$TMPDIR/org_repos.txt" 2>/dev/null &
    gh repo list ErikBjare --no-archived --source --json nameWithOwner,pushedAt --limit 10 > "$TMPDIR/personal_repos.json" 2>/dev/null &
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
        ((i++))
    done <<< "$all_repos"
    wait

    # Print results in order
    for f in "$TMPDIR"/[0-9]*.txt; do
        [ -f "$f" ] && cat "$f"
    done
fi
