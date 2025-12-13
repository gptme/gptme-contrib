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
Stage only intended files explicitly, verify branch before committing, and commit inside submodules before updating superproject pointers.

## Context
When committing changes via git.

## Detection
Observable signals indicating git workflow issues:
- Using `git add .` or `git commit -a` (stages unintended files)
- Creating branches/PRs for trivial docs/journal changes
- Committing to master when feature branch was intended
- Secrets/tokens appearing in git status (e.g., gptme.toml)
- Submodule changes not committed inside submodule first
- Pushing without explicit permission for non-trivial changes

## Pattern
Execute systematic git workflow:

```txt
1. Decide scope
   - Trivial docs/journal → commit directly on master
   - Non-trivial/code changes → ask first; then branch + PR if approved

2. Prepare working tree
   - Run `git status` to see exactly what changed
   - If sensitive files touched, `git restore <file>`

3. Verify branch (CRITICAL)
   - Check: `git branch --show-current`
   - If on master but should be feature branch: switch now
   - Recovery: `git branch feature && git reset --hard HEAD~1 && git checkout feature`

4. Commit with explicit paths
   - Tracked files: `git commit path1 path2 -m "message"`
   - Untracked files: `git add path1 && git commit path1 -m "message"`
   - NEVER: `git add .` or `git commit -a`

5. Submodules
   - First: commit inside submodule
   - Then: `git add <submodule>` in superproject

6. Push/PR
   - Don't push unless requested
   - Feature branches: push and create PR with clear title
```

**Correct examples**:
```bash
# Check what changed (shows tracked vs untracked)
git status

# Verify on correct branch
git branch --show-current  # Should show feature-branch, not master

# Tracked files: Commit directly
git commit tasks/task.md lessons/lesson.md -m "docs: update task and lesson"

# Untracked files: Add then commit (explicit in both!)
git add journal/2025-11-06.md && git commit journal/2025-11-06.md -m "docs(journal): summary"

# Mixed: Add untracked, then commit all explicitly
git add journal/new.md && git commit journal/new.md tasks/existing.md -m "docs: session work"
```

## Outcome
Following this pattern results in:
- **Clean commits**: Only intended files staged
- **Protected secrets**: Sensitive files never committed
- **Correct branches**: Work on intended branch
- **Clean submodules**: Changes tracked properly in both repos
- **Reduced review burden**: No PRs for trivial changes

## Related
- [Git Worktree Workflow](./git-worktree-workflow.md) - For working on external PRs (read together!)
- [Git Remote Branch Pushing](./git-remote-branch-pushing.md) - Pushing to upstream branches
- [When to Rebase PRs](./when-to-rebase-prs.md) - When to rebase

## Origin
Established 2025-08-08 from user/agent session to reduce review overhead and PR noise.
