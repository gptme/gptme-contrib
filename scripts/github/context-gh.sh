#!/bin/bash

# Output GitHub context for gptme
# Usage: ./scripts/context-gh.sh

set -e

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "# GitHub Context"
echo

# Check if we're in a git repository
if ! git rev-parse --git-dir > /dev/null 2>&1; then
    echo "Not in a git repository, skipping GitHub section."
    exit 0
fi

# Get the repository from git remote
REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || echo "")

if [ -z "$REPO" ]; then
    echo "Could not determine GitHub repository."
    exit 0
fi

echo "Repository: $REPO"
echo

# List open requests for Erik (typically blockers)
echo "## Open Requests for Erik"
echo
echo "*Items needing Erik's attention, typically blockers or decisions.*"
echo
REQUEST_COUNT=$(gh issue list --repo "$REPO" --label request-for-erik --state open --json number --jq '. | length')

if [ "$REQUEST_COUNT" -eq 0 ]; then
    echo "No open requests."
else
    echo "Found $REQUEST_COUNT open request(s):"
    echo
    gh issue list --repo "$REPO" --label request-for-erik --state open --json number,title,labels,createdAt --jq '.[] | "- #\(.number): \(.title) [\(.labels | map(.name) | join(", "))] (created \(.createdAt | split("T")[0]))"'
fi

echo

# Add GitHub notifications summary
echo "## GitHub Notifications"
echo
echo "*Unread notifications requiring attention.*"
echo
NOTIFICATIONS=$(gh api notifications 2>/dev/null || echo "[]")
NOTIF_COUNT=$(echo "$NOTIFICATIONS" | jq '. | length')

if [ "$NOTIF_COUNT" -eq 0 ]; then
    echo "No unread notifications."
else
    echo "Found $NOTIF_COUNT unread notification(s):"
    echo
    # Show brief summary of notifications
    echo "$NOTIFICATIONS" | jq -r '.[] | "- [\(.subject.type)] \(.repository.full_name): \(.subject.title) (\(.reason))"' | head -10

    if [ "$NOTIF_COUNT" -gt 10 ]; then
        echo "... and $((NOTIF_COUNT - 10)) more (use ./scripts/check-notifications.sh for details)"
    fi
fi

echo

# Add gptme-bob issues (if not already in that repo)
if [ "$REPO" != "ErikBjare/gptme-bob" ]; then
    echo "## gptme-bob Issues"
    echo
    echo "*Open issues in Bob's brain repository.*"
    echo
    BOB_ISSUES=$(gh issue list --repo ErikBjare/gptme-bob --state open --json number,title,labels --jq '. | length')

    if [ "$BOB_ISSUES" -eq 0 ]; then
        echo "No open issues."
    else
        echo "Found $BOB_ISSUES open issue(s):"
        echo
        gh issue list --repo ErikBjare/gptme-bob --state open --json number,title,labels --jq '.[] | "- #\(.number): \(.title) [\(.labels | map(.name) | join(", "))]"'
    fi

    echo
fi

# Add repository CI status using existing script
echo "## Repository CI Status"
echo
echo "*Build health for active repositories. Run \`./scripts/repo-status.sh\` for details.*"
echo

# Run repo status script and capture output (strip colors)
"$SCRIPT_DIR/repo-status.sh" 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' | grep -E "^(Checking|===|  └─|No open|#[0-9])" | head -20 || echo "Unable to fetch CI status"

echo

# List recent PRs
echo "## Recent Pull Requests"
echo
gh pr list --repo "$REPO" --limit 5 --json number,title,state,author,createdAt --jq '.[] | "- #\(.number): \(.title) [\(.state)] by @\(.author.login) (created \(.createdAt | split("T")[0]))"'

echo
