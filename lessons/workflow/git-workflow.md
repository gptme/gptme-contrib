---
match:
  keywords:
    - git workflow
    - branch management
    - PR creation
    - git add
    - git restore
    - master branch
    - feature branch
    - Conventional Commits
    - submodule update
    - secret protection
    - git status
    - commit
    - stage
    - checkout
    - push
---

# Git Workflow

## Rule
Stage only intended files, commit with explicit paths, verify branch before committing, and follow scope-based PR decisions.

## Context
When committing changes via git in Bob's workspace or external repositories.

## Detection
Observable signals indicating need for careful git workflow:
- About to use `git add .` or `git commit -a`
- Making changes without checking `git status` first
- Creating branch/PR for trivial docs change
- Committing without verifying current branch
- Editing files that might contain secrets (gptme.toml)
- Working with submodules

## Pattern
Follow scope-based workflow:

**Scope Decision**:
- Small docs/journal tweaks: commit directly on master (no PR)
- Non-trivial/behavioral/code changes: ask first; if approved, branch + PR
- Never use `git add .` or `git commit -a`
- Use Conventional Commits format

**Commit Workflow**:
```bash
# 1. Check what changed
git status

# 2. Verify on correct branch
git branch --show-current

# 3. Restore any sensitive files
git restore gptme.toml  # If touched

# 4. Commit with explicit paths
# Tracked files:
git commit path1 path2 -m "docs: update files"

# Untracked files (must add first):
git add journal/2025-11-06.md && git commit journal/2025-11-06.md -m "docs(journal): session"
```

**Recovery from accidental master commit**:
```bash
git branch feature-branch    # Create branch at current HEAD
git reset --hard HEAD~1      # Move master back
git checkout feature-branch  # Switch to feature branch
```

**Submodules**:
- Commit inside submodule first
- Then in superproject: `git add <submodule>` and commit

## Outcome
Following this pattern results in:
- **Clean history**: Only intended files committed
- **No secrets leaked**: Sensitive files restored before commit
- **Appropriate PRs**: Right scope for each change type
- **Safe operations**: Branch verified before commit

## Related
- [Git Worktree Workflow](./git-worktree-workflow.md) - For working on external PRs (read together!)
- [Git Remote Branch Pushing](./git-remote-branch-pushing.md) - Pushing to upstream branches
- [When to Rebase PRs](./when-to-rebase-prs.md) - When to rebase

## Origin
Established 2025-08-08 from user/agent session to reduce review overhead and PR noise.
