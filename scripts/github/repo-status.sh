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
    local label=${2:-$(basename "$repo")}

    # Get latest workflow run
    local status
    status=$(gh run list --repo "$repo" --limit 1 --json conclusion -q '.[0].conclusion' 2>/dev/null || echo "unknown")

    case "$status" in
        "success")
            echo -e "${GREEN}✓${NC} $label: Passing"
            ;;
        "failure")
            echo -e "${RED}✗${NC} $label: Failing"
            # Get the workflow URL for easy access to logs
            local workflow_url
            workflow_url=$(gh run list --repo "$repo" --limit 1 --json url -q '.[0].url' 2>/dev/null || echo "")
            if [ -n "$workflow_url" ]; then
                echo "  $workflow_url"
            fi
            ;;
        "cancelled"|"skipped")
            echo -e "${YELLOW}⚠${NC} $label: $status"
            ;;
        *)
            echo "? $label: Unknown status"
            ;;
    esac
}

echo "=== Repository CI Status ==="
echo

# If arguments provided, use them as repos
if [ $# -gt 0 ]; then
    # Process repos from arguments
    # Format: "owner/repo:label" or just "owner/repo" (label defaults to repo name)
    for arg in "$@"; do
        if [[ "$arg" == *":"* ]]; then
            repo="${arg%:*}"
            label="${arg#*:}"
            check_repo "$repo" "$label"
        else
            check_repo "$arg"
        fi
    done
else
    # Try to use watched repos as fallback
    watched_repos=$(gh api --paginate /user/subscriptions 2>/dev/null | jq -r '.[].full_name' 2>/dev/null || echo "")

    if [ -n "$watched_repos" ]; then
        # Use watched repos
        echo "$watched_repos" | while read -r repo; do
            [ -n "$repo" ] && check_repo "$repo"
        done
    else
        # Final fallback: Default repos (gptme ecosystem)
        check_repo "gptme/gptme" "gptme"
        check_repo "gptme/gptme-rag" "gptme-rag"
        check_repo "gptme/gptme-webui" "gptme-webui"
        check_repo "gptme/gptme-agent-template" "gptme-agent-template"
        check_repo "gptme/gptme-landing" "gptme-landing"
    fi
fi

# Check for any open PRs (if user is available)
if [ -n "$GH_USER" ]; then
    echo
    echo "=== Open PRs ==="
    echo

    prs=$(gh search prs --author="$GH_USER" --state=open --json repository,number,title,url 2>/dev/null || echo "[]")

    if [ "$prs" == "[]" ] || [ -z "$prs" ]; then
        echo "No open PRs"
    else
        echo "$prs" | jq -r '.[] | "\(.repository.nameWithOwner) #\(.number): \(.title)\n  \(.url)"'
    fi

    echo
fi
