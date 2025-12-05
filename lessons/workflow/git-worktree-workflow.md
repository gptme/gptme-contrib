---
match:
  keywords:
  - git
  - worktree
  - PR
  - external
  - checkout
  - branch
  - pull request
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

## Context
When making changes to repositories you don't own (gptme, gptme-contrib, etc.) where you need to create PRs.

## Detection
Observable signals that you need worktrees:
- About to create a branch in main repository directory
- Blocking on one feature while wanting to work on another
- Multiple agents/runs could conflict on same branch
- Working on external repo where you need to create PR

- About to implement a feature that might already exist in the codebase
- Received request to "add" or "extend" functionality without verifying current capabilities
- Resuming multi-phase task where main task file shows phase completion but lacks implementation details
- Need to understand what was actually implemented vs. what was skipped in previous sessions
- Resuming multi-phase task where main task file shows phase completion but lacks implementation details
- Need to understand what was actually implemented vs. what was skipped in previous sessions
- Spent 15-20+ minutes on same problem without progress
- Multiple failed attempts (2-3+) at solving an issue
- Unclear how to proceed with a technical challenge
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

**Historical incidents**:
- Session 1295 (2025-11-24): Image Gen Phase 3.1 pushed to master
- Session 1296 (2025-11-24): Image Gen Phase 3.2 pushed to master
- Root cause: Used `git worktree add -b name origin/master` without unsetting upstream

**Before implementing new features - Check existing capabilities**:
```shell
# 1. Check imports for relevant libraries (yaml, json, etc.)
cat tool.py | head -100
grep "^import\|^from" tool.py

# 2. Check main() or CLI for existing flags
grep "def main" tool.py -A 50
grep "argparse\|--config\|--" tool.py

# 3. Search for implementation patterns
grep "if config:\|load.*yaml\|parse.*config" tool.py

# 4. Check for partial implementations
grep -n "TODO\|FIXME\|NotImplemented" tool.py
```

**Result**: Often reveals feature already exists, reducing work from "implement feature" to "use existing feature" or "add one config entry".

**Checking phase status documents**:
```shell
# When resuming multi-phase tasks, check for detailed status files
ls knowledge/*-phase*-status.md

# Example: Main task shows "Phase 2.2 complete" but unclear what that means
cat knowledge/implement-unified-message-system-phase2-status.md

# Look for:
# - Session-by-session progress (Session 789, 790, etc.)
# - Implementation decisions ("decided to SKIP rate limiting")
# - Actual completion details beyond main task file
# - What was implemented vs. what was deferred
```

**Why phase status files matter**:
- Main task files show high-level phase completion
- Status files contain session-specific decisions and context
- Critical details like "SKIP X" or "defer Y" often only in status files
- Reveals actual implementation state for resuming work

## When Stuck: Research Before Extended Stumbling

If you encounter problems during worktree workflow:

```shell
# After 15-20 minutes stuck OR 2-3 failed attempts:
# 1. Stop and research the specific issue
#    Use Perplexity or other research tools
#    Example queries:
#    - "git worktree branch tracking not working"
#    - "gh pr checkout sets upstream automatically"
#    - "git push fatal: no upstream branch"

# 2. Apply solution found through research
# 3. Document what worked in commit message or notes
```

**Why this matters**:
- Prevents 20+ minute stumbling sessions
- Leverages existing knowledge from community
- Research can resolve issues in ~2 minutes vs extended trial-and-error
- Accelerates learning by understanding root cause

## Outcome
Following this pattern results in:
- **Parallel work**: Multiple features simultaneously
- **Clean separation**: Each feature has own directory
- **No duplicates**: Checking first avoids duplicates
- **Safe base**: origin/master prevents accidental commits

- **Avoid duplicate work**: Discover existing implementations before building
- **Faster completion**: Use existing features instead of reimplementing
- **Better code quality**: Leverage tested, existing functionality
- **Better context**: Session-specific decisions and implementation details
- **Avoid rework**: Understand what was skipped vs. completed
- **Faster problem resolution**: Research resolves blocks in ~2 minutes vs 20+ minutes of stumbling
- **Reduced frustration**: Avoid repeated failed attempts on same issue
## Related
- [Git Workflow](./git-workflow.md) - Commit practices and branch verification (read together!)
- [When to Rebase PRs](./when-to-rebase-prs.md) - Rebase workflow
- [Git Remote Branch Pushing](./git-remote-branch-pushing.md) - Pushing to upstream branches
