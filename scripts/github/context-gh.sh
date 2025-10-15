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

# Add repository CI status using existing script
echo "## Repository CI Status"
echo
echo "*Build health for active repositories. Run \`./scripts/repo-status.sh\` for details.*"
echo
./scripts/github/repo-status.sh

# Show GitHub notifications
echo
echo "## GitHub Notifications"
echo
echo "*Unread notifications requiring attention.*"
echo

# Run notification check script
"$SCRIPT_DIR/check-notifications.sh"
