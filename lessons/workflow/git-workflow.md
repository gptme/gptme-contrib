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
lesson_id: workflow_git-workflow_49086c79
version: 1.0.0
usage_count: 0
helpful_count: 0
harmful_count: 0
created: '2025-11-04T18:14:42.679244Z'
updated: '2025-11-04T18:14:42.679244Z'
last_used: null
---

# Git Workflow

## Context
When committing changes via git.

## Problem
Unnecessary branches/PRs for trivial changes, staging too much (git add .), leaking secrets, and submodule noise increase friction and review overhead.

## Defaults
- Small docs/journal tweaks: commit directly on master (no PR).
- Non-trivial/behavioral/code changes: ask first; if approved, branch + PR.
- Stage only intended files; never use `git add .` or `git commit -a`.
- Use Conventional Commits; single, clear commit for small changes.
- Never commit secrets/tokens (e.g., gptme.toml env); restore if edited.
- Submodules: commit inside the submodule first, then update the superproject pointer.
- Don’t push or create PRs unless requested.

## Step-by-step workflow
1) Decide scope
   - If trivial docs/journal → commit on master.
   - If non-trivial/needs review → ask; then branch + PR if approved.
   - Avoid: creating branches/PRs for tiny edits.

2) Prepare working tree
   - Run `git status` to see exactly what changed.
   - If sensitive files (e.g., gptme.toml) were touched, `git restore <file>`.
   - Avoid: carrying unrelated/untracked files into commits.

3) Verify branch (prevents accidental master commits)
   - Check current branch: `git branch --show-current`
   - If on master but should be on feature branch: switch now
   - If accidentally on master: don't commit, create proper branch first
   - Avoid: committing to master when feature branch intended

4) Commit with explicit paths (no staging)
   - Commit files directly: `git commit path1 path2 -m "type(scope): message"`
   - This prevents accidentally committing staged files you weren't aware of
   - If you already staged files: review `git status` carefully before committing
   - Avoid: `git add .` then `git commit` (can commit unintended staged changes)
   - Avoid: `git commit -a` (commits all tracked changes)

Example correct workflow:
```bash
# Check what changed
git status

# Verify on correct branch
git branch --show-current  # Should show feature-branch, not master

# Commit only intended files
git commit journal/2025-11-04.md tasks/my-task.md -m "docs: update journal and task"

# Co-authorship
git commit --amend --no-edit --trailer "Co-authored-by: Bob <bob@superuserlabs.org>"
```

Recovery from accidental master commit:
```bash
# If you committed to master by accident:
git branch feature-branch    # Create branch at current HEAD
git reset --hard HEAD~1      # Move master back one commit
git checkout feature-branch  # Switch to feature branch
# Now you're on feature-branch with your commit, master is clean
```

5) Submodules (when applicable)
   - In the submodule: commit the actual file changes.
   - In the superproject: `git add <submodule>` and commit (“chore: bump …”).
   - Avoid: editing submodule files only from the superproject without committing inside the submodule.

6) Push/PR
   - Don’t push or open PRs unless requested.
   - If on a feature branch and review is desired: push and open PR with a clear title/body.
   - Avoid: pushing master without confirmation for non-trivial changes.

## Related
- [Git Worktree Workflow](./git-worktree-workflow.md) - For working on external PRs (read together!)
- [Git Remote Branch Pushing](./git-remote-branch-pushing.md) - Pushing to upstream branches
- [When to Rebase PRs](./when-to-rebase-prs.md) - When to rebase

## Origin
Established 2025-08-08 from Erik/Bob session to reduce review overhead and PR noise.
