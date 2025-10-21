#!/bin/bash
# Generates comprehensive GitHub context for AI agents
# Includes notifications, issues, PRs, and CI status
# Part of gptme-contrib GitHub integration scripts

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Get current repository (if in git repo)
if git rev-parse --git-dir > /dev/null 2>&1; then
    REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || echo "")
else
    REPO=""
fi

# Only show repo context if we're in a git repository
if [ -n "$REPO" ]; then
    echo "# GitHub Context"
    echo
    echo "Repository: $REPO"
    echo
fi

# PRIMARY: What Bob is asked to help with
# - gptme-bob issues (requests, assignments)
# - Direct mentions asking for help
# These should be checked FIRST in autonomous runs

# Show GitHub notifications (includes gptme-bob issues via mentions/assignments)
echo "## GitHub Notifications"
echo
echo "*Unread notifications requiring attention.*"
echo

# Run notification check script with filtering for closed/merged items
"$SCRIPT_DIR/check-notifications.sh" --only-open

# SECONDARY: Bob's own work status
# - Repository health checks
# - Bob's open PRs
# Check these only when PRIMARY sources are blocked

# Add repository CI status using existing script
echo
echo "## Repository CI Status"
echo
echo "*Build health for active repositories. Run \`$SCRIPT_DIR/repo-status.sh\` for details.*"
echo
"$SCRIPT_DIR/repo-status.sh"

# Show Bob's open PRs
GH_USER="${GH_USER:-$(gh api user -q .login 2>/dev/null || echo "")}"
if [ -n "$GH_USER" ]; then
    echo
    echo "## Open PRs"
    echo

    prs=$(gh search prs --author="$GH_USER" --state=open --json repository,number,title,url 2>/dev/null || echo "[]")

    if [ "$prs" == "[]" ] || [ -z "$prs" ]; then
        echo "No open PRs"
    else
        echo "$prs" | jq -r '.[] | "\(.repository.nameWithOwner) #\(.number): \(.title)\n  \(.url)"'
    fi
fi
