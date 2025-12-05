---
match:
  keywords:
  - about to create PR
  - gh pr create
  - git checkout -b fix-
  - gh pr list --search
  - duplicate PR
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
