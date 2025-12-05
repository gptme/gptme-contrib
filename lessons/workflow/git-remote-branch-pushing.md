---
match:
  keywords:
  - git
  - push
  - branch
  - remote
  - PR
lesson_id: workflow_git-remote-branch-pushing_aa405aaf
version: 1.0.0
usage_count: 1
helpful_count: 1
harmful_count: 0
created: '2025-11-04T18:14:42.677760Z'
updated: '2025-11-04T18:24:16.449224Z'
last_used: '2025-11-04T18:24:16.449224Z'
---

# Git Remote Branch Pushing for PRs

## Rule
Always explicitly specify the remote branch name when pushing new branches for PRs, don't rely on default push behavior.

## Context
When creating pull requests by pushing a new local branch to a remote repository.

## Detection
Observable signals that you need explicit branch specification:
- About to create a PR from a new local branch
- Pushing to external repository (not workspace)
- Want to create feature branch on remote
- Using `git push origin <branch>` without verifying destination

Common error pattern:
```shell
git checkout -b feature-branch
git push origin feature-branch
# Silently pushes to wrong destination (e.g., master)
```

## Pattern
Explicitly specify both source and destination:
```shell
# Create local branch
git checkout -b feature-branch

# CORRECT: Explicit source and destination
git push origin feature-branch:feature-branch
# OR: Set upstream tracking
git push -u origin feature-branch

# WRONG: Relying on default behavior
git push origin feature-branch  # May push to wrong branch
```

**Verification** after push:
```shell
# Verify what was pushed
git branch -r | grep feature-branch
# Should show: origin/feature-branch

# Check PR can be created
gh pr create --fill
```

## Outcome
Following this pattern prevents:
- Pushing to wrong remote branch (e.g., master instead of feature branch)
- Creating conflicts on protected branches
- Having to revert incorrect pushes
- Breaking CI/PR workflow

Benefits:
- Explicit destination prevents errors
- Clear in

tent documented in command
- Upstream tracking set correctly
- PRs created from correct branch

## Related
- [Git Worktree Workflow](./git-worktree-workflow.md)
- [Git Workflow](./git-workflow.md)

## Origin
2025-11-03 Session 663: Attempted to create revert PR but `git push origin revert-communication-utils` pushed to master instead of creating new branch on remote. This caused the revert commit to go directly to master, repeating the original mistake of pushing directly to master.
