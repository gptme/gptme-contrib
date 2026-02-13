---
match:
  keywords:
    - messy PR
    - unrelated commits
    - clean PR
    - cherry-pick commits
    - PR hygiene
status: active
---

# Clean PR Creation

## Rule
Always create feature branches from `origin/master`, not from local branches that may have accumulated other work. If a PR becomes messy with unrelated commits, create a clean branch and cherry-pick only the relevant commits.

## Context
When creating PRs for specific features or fixes, especially after working on multiple things in the same repository.

## Detection
Observable signals that you need this rule:
- PR contains commits unrelated to the PR topic
- Maintainer says "you messed this up by including a bunch of unrelated commits"
- PR description doesn't match all commits in the PR
- Branch was created from a local branch instead of origin/master

## Pattern

**Prevention** (always do this):
```shell
# CORRECT: Create branch from origin/master
git fetch origin
git checkout -b feature-name origin/master

# WRONG: Create from local master (may have uncommitted work)
git checkout master
git checkout -b feature-name
```

**Recovery** (if PR is already messy):
```shell
# 1. Create clean branch from origin/master
git fetch origin
git checkout -b feature-clean origin/master

# 2. Cherry-pick only the relevant commits
git cherry-pick <commit-hash-1>
git cherry-pick <commit-hash-2>

# 3. Push and create new PR
git push -u origin feature-clean
gh pr create --title "..." --body "..."

# 4. Close messy PR with explanation
gh pr close <messy-pr-number> --comment "Closing in favor of #<clean-pr-number> which has only the relevant commits."
```

## Example

**Scenario**: PR #281 had 10 commits - 8 voice interface commits mixed with 2 Twitter fix commits.

**Recovery**:
1. Created `fix-twitter-draft-limit-clean` from origin/master
2. Cherry-picked only the 2 Twitter fix commits
3. Created PR #282
4. Closed PR #281 with explanation

**Result**: Clean PR with only relevant commits, easier for maintainer to review.

## Outcome
Following this pattern results in:
- **Easier reviews**: Maintainers see only relevant changes
- **Professional workflow**: Clear PR history
- **Faster merges**: No confusion about what's included
- **Better collaboration**: Respects maintainer's time

## Related
- [Git Worktree Workflow](./git-worktree-workflow.md) - Branch from origin/master
- [Git Workflow](./git-workflow.md) - General commit practices

## Origin
2026-02-13: PR #281 had unrelated voice interface commits mixed with Twitter fix. Created clean PR #282 with only relevant commits.
