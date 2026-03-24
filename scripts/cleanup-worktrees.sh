#!/usr/bin/env bash
# Cleanup stale worktrees in /tmp/worktrees/ that correspond to merged/closed PRs.
#
# Usage:
#   ./scripts/cleanup-worktrees.sh           # Dry run (show what would be deleted)
#   ./scripts/cleanup-worktrees.sh --delete  # Actually delete stale worktrees
#
# Checks each worktree's branch against GitHub PRs to determine if the
# associated PR has been merged or closed. Only deletes worktrees where
# the PR is definitively merged/closed.
#
# Environment variables:
#   WORKTREE_DIR   Directory containing worktrees (default: /tmp/worktrees)
#   GH_USERNAME    GitHub username for cross-fork PR lookup
#                  (default: auto-detected via `gh api user`)

set -euo pipefail

WORKTREE_DIR="${WORKTREE_DIR:-/tmp/worktrees}"
DRY_RUN=true

if [[ "${1:-}" == "--delete" ]]; then
    DRY_RUN=false
fi

if [[ ! -d "$WORKTREE_DIR" ]]; then
    echo "No worktree directory found at $WORKTREE_DIR"
    exit 0
fi

# Auto-detect GitHub username for fork PR lookup
GH_USERNAME="${GH_USERNAME:-$(gh api user --jq '.login' 2>/dev/null || echo "")}"

SAFE_TO_DELETE=()
KEEP=()
UNKNOWN=()

for dir in "$WORKTREE_DIR"/*/; do
    [[ -d "$dir" ]] || continue
    name=$(basename "$dir")

    # Skip non-git directories
    if ! git -C "$dir" rev-parse --git-dir &>/dev/null; then
        UNKNOWN+=("$name (not a git repo)")
        continue
    fi

    branch=$(git -C "$dir" branch --show-current 2>/dev/null || echo "")
    remote=$(git -C "$dir" remote get-url origin 2>/dev/null || echo "")

    if [[ -z "$branch" || -z "$remote" ]]; then
        UNKNOWN+=("$name (no branch or remote)")
        continue
    fi

    # Extract owner/repo from remote URL
    repo=""
    if [[ "$remote" =~ github\.com[:/]([^/]+/[^/.]+) ]]; then
        repo="${BASH_REMATCH[1]}"
        repo="${repo%.git}"
    fi

    if [[ -z "$repo" ]]; then
        UNKNOWN+=("$name (can't parse repo from $remote)")
        continue
    fi

    # Check for PR with this branch — try the repo first, then upstream if it's a fork
    pr_info=$(gh pr list --repo "$repo" --state all --head "$branch" \
        --json number,state --jq '.[0] | "\(.number) \(.state)"' 2>/dev/null || echo "")

    # If no PR found and username is known, check upstream org repos with cross-fork syntax
    if [[ -z "$pr_info" && -n "$GH_USERNAME" && "$repo" == "$GH_USERNAME"/* ]]; then
        upstream_name="${repo#"$GH_USERNAME"/}"
        for org in gptme ActivityWatch; do
            pr_info=$(gh pr list --repo "$org/$upstream_name" --state all \
                --head "$GH_USERNAME:$branch" \
                --json number,state --jq '.[0] | "\(.number) \(.state)"' 2>/dev/null || echo "")
            [[ -n "$pr_info" ]] && repo="$org/$upstream_name" && break
        done
    fi

    if [[ -z "$pr_info" ]]; then
        UNKNOWN+=("$name (branch=$branch, repo=$repo, no PR found)")
        continue
    fi

    pr_number=$(echo "$pr_info" | awk '{print $1}')
    pr_state=$(echo "$pr_info" | awk '{print $2}')

    # Handle null/empty results from jq on empty arrays
    if [[ "$pr_number" == "null" || -z "$pr_state" || "$pr_state" == "null" ]]; then
        UNKNOWN+=("$name (branch=$branch, repo=$repo, no matching PR)")
        continue
    fi

    dir_size=$(du -sh "$dir" 2>/dev/null | awk '{print $1}')

    if [[ "$pr_state" == "MERGED" || "$pr_state" == "CLOSED" ]]; then
        SAFE_TO_DELETE+=("$name")
        echo "✓ DELETE: $name (PR #$pr_number $pr_state, $dir_size)"
    else
        KEEP+=("$name")
        echo "  KEEP:   $name (PR #$pr_number $pr_state)"
    fi
done

echo ""
echo "=== Summary ==="
echo "Safe to delete: ${#SAFE_TO_DELETE[@]}"
echo "Keep (open PRs): ${#KEEP[@]}"
echo "Unknown: ${#UNKNOWN[@]}"

if [[ ${#UNKNOWN[@]} -gt 0 ]]; then
    echo ""
    echo "Unknown worktrees (review manually):"
    for item in "${UNKNOWN[@]}"; do
        echo "  ? $item"
    done
fi

if [[ ${#SAFE_TO_DELETE[@]} -eq 0 ]]; then
    echo ""
    echo "Nothing to clean up."
    exit 0
fi

if $DRY_RUN; then
    echo ""
    echo "Dry run — pass --delete to actually remove stale worktrees."
else
    echo ""
    echo "Deleting ${#SAFE_TO_DELETE[@]} stale worktrees..."
    for name in "${SAFE_TO_DELETE[@]}"; do
        rm -rf "${WORKTREE_DIR:?}/$name"
        echo "  Deleted: $name"
    done

    after_size=$(du -sh "$WORKTREE_DIR" 2>/dev/null | awk '{print $1}')
    echo ""
    echo "Done. Remaining worktree dir size: $after_size"
fi
