---
match:
  keywords:
  # FOUNDATIONAL KEYWORDS - Keep these general terms!
  # These ensure the lesson triggers during common git/PR workflows.
  # Do NOT remove thinking they're "too broad" - they're intentionally
  # general to catch workflows where users SHOULD learn this pattern.
  - "git"
  - "worktree"
  - "PR"
  - "external"
  - "branch"
  # PROBLEM SIGNALS - Specific triggers for when problems occur
  - "branch from origin/master"
  - "included uncommitted changes"
  # WORKFLOW CONTEXT - Medium specificity for discussions
  - "git worktree for PR"
  - "external repository PR"
  - "multiple branches simultaneously"
lesson_id: workflow_git-worktree-workflow_15082b29
version: 1.0.0
usage_count: 1
helpful_count: 1
harmful_count: 0
created: '2025-11-04T18:14:42.680660Z'
updated: '2025-11-04T18:24:16.449224Z'
last_used: '2025-11-04T18:24:16.449224Z'
---

# Git Worktree Workflow for External Repositories

## Rule

**ALWAYS branch from `origin/master`, NEVER from local `master`.**
When creating new work, always `git fetch origin master` first, then create branch from `origin/master`.
This prevents accidentally including uncommitted/unmerged changes.

## Quick Start: Single-Command Workflow

**For existing PRs** (checkout and resume work):
```shell
# Run from repo root (e.g., ~/gptme)
PR=123 && \
BRANCH=$(gh pr view $PR --json headRefName -q .headRefName) && \
git fetch origin && \
(git worktree list | grep -q "worktree/$BRANCH" || git worktree add "worktree/$BRANCH") && \
cd "worktree/$BRANCH" && \
gh pr checkout $PR
```

**For new work** (create branch and worktree):
```shell
# Run from repo root
BRANCH="feature-name" && \
git fetch origin && \
(git worktree list | grep -q "worktree/$BRANCH" || git worktree add "worktree/$BRANCH" -b "$BRANCH" origin/master) && \
cd "worktree/$BRANCH" && \
git branch --unset-upstream && \
git push -u origin "$BRANCH"
```

**Key points**:
- `gh pr checkout` handles branch tracking automatically for existing PRs
- The `grep -q` check reuses existing worktrees instead of failing
- For new branches, `--unset-upstream` prevents accidental push to master

## Context
When making changes to repositories you don't own (gptme, gptme-contrib, etc.) where you need to create PRs.

## Detection
Observable signals that you need worktrees:
- About to create a branch in main repository directory
- Blocking on one feature while wanting to work on another
- Multiple agents/runs could conflict on same branch
- Working on external repo where you need to create PR

## Pattern
Core workflow using ORIGINAL upstream branch names:
```shell
# 1. Get original branch name from PR (critical)
gh pr view 812 --json headRefName -q .headRefName
# Output: feature-task-loop (this is what we use)

# 2. Check if worktree exists with that name
cd ~/gptme
git worktree list | grep feature-task-loop

# 3. Create worktree with ORIGINAL branch name (only if doesn't exist)
# CORRECT: Use upstream's branch name and let gh pr checkout set tracking
git worktree add worktree/feature-task-loop
cd worktree/feature-task-loop
gh pr checkout 812  # Fetches PR and sets up tracking automatically

# ALTERNATIVE: If creating new branch (not checking out existing PR)
# git worktree add worktree/feature-name -b feature-name origin/master
# git push -u origin feature-name  # -u sets upstream tracking

# WRONG: Don't use pr-NUMBER format
# git worktree add worktree/pr-812 -b pr-812 origin/master  # ❌ Breaks tracking

# 4. Do work, commit, push (tracking already set by gh pr checkout)
git push  # Pushes to correct upstream branch
gh pr create

# 5. After merge, cleanup
cd ~/gptme
git worktree remove worktree/feature-task-loop
```

**Why original branch names matter**:
- PR already tracks the original branch (e.g., "feature-task-loop")
- Creating "pr-812" branch breaks this tracking
- When resuming work, checking out "pr-812" doesn't match PR branch
- Causes confusion about which branch corresponds to which PR

**Getting the original branch name**:
```shell
# From PR URL or number
gh pr view 812 --json headRefName -q .headRefName
gh pr view https://github.com/gptme/gptme/pull/812 --json headRefName -q .headRefName
```

**⚠️ CRITICAL: Branch Tracking Issue and Fix**:

When creating a new worktree with `-b branch-name origin/master`, the branch automatically tracks `origin/master`. This causes `git push` to push directly to master instead of creating a new remote branch, bypassing PR workflow.

**Symptoms**:
- You create a worktree: `git worktree add worktree/feature -b feature origin/master`
- You make commits and push: `git push`
- Changes go directly to `origin/master` (WRONG!)
- No PR created, master polluted with unreviewed changes

**The Problem**:
```shell
# After this command, 'feature' branch tracks origin/master
git worktree add worktree/feature -b feature origin/master

# Check the tracking:
git branch -vv
# * feature  abc1234 [origin/master] Latest commit  ← BAD: tracking master!
```

**The Fix** (use ONE of these approaches):

**Option 1**: Explicit unset before first push (safest)
```shell
# After creating worktree and making commits:
cd worktree/feature
git branch --unset-upstream  # Remove tracking
git push -u origin feature   # Push to NEW remote branch, set tracking
```

**Option 2**: Create worktree without initial tracking (recommended for NEW work)
```shell
# Step 1: Create worktree pointing to origin/master (no local branch yet)
git worktree add worktree/feature

# Step 2: Create and checkout branch (no upstream set automatically)
cd worktree/feature
git checkout -b feature origin/master

# Step 3: First push sets up tracking correctly
git push -u origin feature
```

**Option 3**: Use `gh pr checkout` for existing PRs (automatic tracking)
```shell
# For existing PRs, gh pr checkout handles tracking correctly:
git worktree add worktree/feature
cd worktree/feature
gh pr checkout 123  # Sets up tracking to PR's branch automatically
```

**Verification** before pushing:
```shell
# Always check branch tracking before first push:
git branch -vv
# Should show [origin/feature] NOT [origin/master]

# If it shows [origin/master], unset it:
git branch --unset-upstream
git push -u origin feature
```

## Outcome
Following this pattern results in:
- **Parallel work**: Multiple features simultaneously
- **Clean separation**: Each feature has own directory
- **No duplicates**: Checking first avoids duplicates
- **Safe base**: origin/master prevents accidental commits

## Related
- [Git Workflow](./git-workflow.md) - Commit practices and branch verification (read together!)
- [When to Rebase PRs](./when-to-rebase-prs.md) - Rebase workflow
- [Git Remote Branch Pushing](./git-remote-branch-pushing.md) - Pushing to upstream branches
- [dotfiles/README.md](../../dotfiles/README.md) - Global git hooks setup (pre-commit, pre-push protection)
