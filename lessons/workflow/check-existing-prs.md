---
match:
  keywords:
  - gh api graphql
  session_categories: [cross-repo, code, triage]
status: active
---

# Check for Existing PRs Before Creating

## Rule
Always check for existing PRs addressing the same issue before creating a new PR.

## Context
When investigating an issue and planning to create a PR to fix it.

## Detection
Observable signals indicating you should check first:
- About to create PR for an issue
- Planning work that seems like common fix
- Issue has been open for a while (likely someone started work)
- Issue has discussion/activity (coordination happening)

## Pattern
Check before creating:
```bash
# Search by issue number and topic
gh pr list --state open --search "605 in:body"
gh pr list --state open --search "mcp config"

# Also check GitHub's issue -> closing PR metadata
gh api graphql \
  -f owner=OWNER \
  -f repo=REPO \
  -F issue=605 \
  -f query='query($owner:String!, $repo:String!, $issue:Int!) {
    repository(owner:$owner, name:$repo) {
      issue(number:$issue) {
        closedByPullRequestsReferences(first:10) {
          nodes { number title url state }
        }
      }
    }
  }' \
  --jq '.data.repository.issue.closedByPullRequestsReferences.nodes[]'

# Do not use /issues/605 --jq '.pull_request'; that only says whether 605 is itself a PR.

# If found: Review and coordinate
# If not found: Proceed with new PR
git checkout -b fix-issue-605
gh pr create
```

## Outcome
Following this pattern leads to:
- **No duplicate work**: Saves hours of wasted effort
- **Better coordination**: Build on others' insights
- **Community respect**: Shows consideration for workflow
- **Cleaner PRs**: No confusion about which to merge

Time saved: Finding existing PR takes 30 seconds vs hours of duplicate work.

## Related
- [Read Full GitHub Context](./read-full-github-context.md) - Complete PR reading
- [GitHub Issue Engagement](../social/github-issue-engagement.md) - Issue workflow
