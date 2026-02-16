---
match:
  keywords:
  - "git workflow"
  - "branch management"
  - "pr creation"
  - "git add"
  - "git restore"
  - "master branch"
  - "feature branch"
  - "conventional commits"
  - "submodule update"
  - "secret protection"
  - "git status"
  - "commit message format"
  - "git commit"
  - "git push"
  - "git checkout"
  - "stage files"
---

# Git Workflow

## Rule
Stage only intended files explicitly, never use `git add .` or `git commit -a`, and commit trivial docs/journal directly to master while using branches/PRs for non-trivial changes.

## Context
When committing changes via git.

## Detection
Observable signals that indicate need for proper git workflow:
- Creating unnecessary branches/PRs for trivial changes
- Using `git add .` which stages unintended files
- Leaking secrets (e.g., gptme.toml) into commits
- Submodule noise from improper update sequence
- Committing to master when feature branch intended

## Pattern
Follow this step-by-step workflow:

**1) Decide scope**
- If trivial docs/journal → commit on master.
- If non-trivial/needs review → ask; then branch + PR if approved.
- Avoid: creating branches/PRs for tiny edits.

**2) Prepare working tree**
- Run `git status` to see exactly what changed.
- If sensitive files (e.g., gptme.toml) were touched, `git restore <file>`.
- Avoid: carrying unrelated/untracked files into commits.

**3) Verify branch** (prevents accidental master commits)
- Check current branch: `git branch --show-current`
- If on master but should be on feature branch: switch now
- If accidentally on master: don't commit, create proper branch first

**4) Commit with explicit paths**
- **For tracked files**: `git commit path1 path2 -m "message"`
- **For untracked files**: `git add path1 path2 && git commit path1 path2 -m "message"`
- Avoid: `git add .` then `git commit` (can commit unintended staged changes)
- Avoid: `git commit -a` (commits all tracked changes)

Example correct workflows:
```bash
# Check what changed (shows tracked vs untracked)
git status

# Verify on correct branch
git branch --show-current  # Should show feature-branch, not master

# Tracked files: Commit directly
git commit tasks/existing-task.md lessons/workflow/some-lesson.md -m "docs: update task and lesson"

# Untracked files: Add then commit (explicit in both!)
git add journal/2025-11-06-topic.md && git commit journal/2025-11-06-topic.md -m "docs(journal): session summary"
git add people/new-person.md && git commit people/new-person.md -m "docs(people): add profile"

# Mixed (untracked + tracked): Add untracked files, then commit all explicitly
git add journal/2025-11-06-topic.md && git commit journal/2025-11-06-topic.md tasks/existing-task.md -m "docs: session work"

# If pre-commit fails, run the entire `git commit` command again (don't amend)
```

Recovery from accidental master commit:
```bash
# If you committed to master by accident:
git branch feature-branch    # Create branch at current HEAD
git reset --hard HEAD~1      # Move master back one commit
git checkout feature-branch  # Switch to feature branch
# Now you're on feature-branch with your commit, master is clean
```

**5) Submodules** (when applicable)
- In the submodule: commit the actual file changes.
- In the superproject: `git add <submodule>` and commit ("chore: bump …").

**6) Push/PR**
- Don't push or open PRs unless requested.
- If on a feature branch and review is desired: push and open PR with a clear title/body.

## Outcome
Following this pattern results in:
- Clean git history with explicit commits
- No accidental staging of unintended files
- Proper branch management (trivial vs non-trivial)
- Protected secrets (restore sensitive files)
- Correct submodule update sequence

## Related
- [Git Worktree Workflow](./git-worktree-workflow.md) - For working on external PRs, including push verification (read together!)
- [When to Rebase PRs](./when-to-rebase-prs.md) - When to rebase

## Origin
Established 2025-08-08 from user/agent session to reduce review overhead and PR noise.
