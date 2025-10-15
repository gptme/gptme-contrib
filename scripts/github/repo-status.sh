#!/bin/bash
# Check CI status across multiple repositories
# Usage: ./scripts/repo-status.sh

set -e

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to check repo status
check_repo() {
    local repo=$1
    local name=$2

    echo -n "Checking $name... "

    # Get latest workflow run
    local status
    status=$(gh run list --repo "$repo" --limit 1 --json status,conclusion,createdAt --jq '.[0] | "\(.status):\(.conclusion)"' 2>/dev/null || echo "error:")

    if [ "$status" == "error:" ]; then
        echo -e "${YELLOW}⚠ Unable to fetch status${NC}"
        return
    fi

    local run_status
    run_status=$(echo "$status" | cut -d: -f1)
    local conclusion
    conclusion=$(echo "$status" | cut -d: -f2)

    if [ "$run_status" == "completed" ]; then
        if [ "$conclusion" == "success" ]; then
            echo -e "${GREEN}✓ Passing${NC}"
        elif [ "$conclusion" == "failure" ]; then
            echo -e "${RED}✗ Failing${NC}"
            # Show the workflow URL for quick access
            local url
            url=$(gh run list --repo "$repo" --limit 1 --json url --jq '.[0].url')
            echo "  └─ $url"
        else
            echo -e "${YELLOW}⚠ $conclusion${NC}"
        fi
    else
        echo -e "${YELLOW}⟳ In progress${NC}"
    fi
}

echo "=== Repository CI Status ==="
echo

# Check repositories in priority order
# Core gptme projects
check_repo "ErikBjare/gptme" "gptme"
check_repo "ErikBjare/gptme-rag" "gptme-rag"

# gptme ecosystem
check_repo "gptme/gptme-webui" "gptme-webui"
check_repo "gptme/gptme-agent-template" "gptme-agent-template"
check_repo "gptme/gptme-landing" "gptme-landing"

# Personal projects
check_repo "TimeToBuildBob/whatdidyougetdone" "whatdidyougetdone"
check_repo "TimeToBuildBob/TimeToBuildBob.github.io" "Website"

# Check for any open PRs Bob has
echo
echo "=== Open PRs ==="
echo

# Check TimeToBuildBob's open PRs across all repos
prs=$(gh search prs --author=TimeToBuildBob --state=open --json repository,number,title,url 2>/dev/null || echo "[]")

if [ "$prs" == "[]" ] || [ -z "$prs" ]; then
    echo "No open PRs"
else
    echo "$prs" | jq -r '.[] | "\(.repository.nameWithOwner) #\(.number): \(.title)\n  \(.url)"'
fi

echo
