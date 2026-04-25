---
match:
  keywords:
    - pr recovery
    - closed pr
    - accidental closure
    - reopen pr
    - force-push closed
    - replace pr
  session_categories: [cross-repo, cleanup]
status: active
---

# PR Recovery After Accidental Closure

## Rule
When a PR is accidentally closed (due to force-push, branch deletion, or other git mishaps), quickly create a replacement PR with clean history rather than trying to reopen the damaged one.

## Context
When working with PRs, accidents can happen — a PR may be accidentally closed due to force-push errors, branch deletion, or other git mishaps. The key is to recover quickly with minimal overhead while preserving all work.

## Detection
Observable signals that you need PR recovery:
- PR shows as "CLOSED" unexpectedly
- Force-push resulted in 0 commits on PR
- Branch was deleted or corrupted
- Attempting to reopen PR fails or shows errors

## Pattern

### Immediate Recovery Steps

**Step 1: Verify commits are intact** (1-2 minutes):
```shell
# Check current PR status
gh pr view <pr-number> --json state,headRefName,baseRefName

# Check if branch still exists locally
git log <feature-branch> --oneline -5

# Check remote branch status
git fetch origin
git log origin/<feature-branch> --oneline -5
```

**Step 2: Create Replacement PR** (3-5 minutes):
```shell
# Fetch latest from origin
git fetch origin

# Create new branch from origin/master (NOT local master)
git checkout -b <new-branch-name> origin/master

# Cherry-pick or copy changes from old branch
# Option A: If old branch exists — identify the commit range first
git log origin/master..<old-branch> --oneline   # shows commits to cherry-pick
git cherry-pick <oldest-commit>^..<old-branch>  # cherry-pick creates commits; no git commit needed

# Option B: Copy files manually if commits are lost
#   git add <files>
#   git commit -m "type(scope): descriptive message"

# Push and create new PR
git push -u origin <new-branch-name>
gh pr create --title "..." --body "Replacement for accidentally closed #<old-pr>. All content preserved."
```

**Step 3: Document the Transition**:
```markdown
## PR Recovery Note

**Original PR**: #<old-pr-number> (accidentally closed)
**Replacement**: this PR
**Reason**: [Brief explanation - force-push error, branch deletion, etc.]
**Status**: All content preserved, clean commit history
```

**Step 4: Close the Loop**:
- Comment on old PR: "Accidentally closed, replaced by #<new-pr>"
- Update any references to point to new PR
- Continue work on replacement PR

## Prevention Strategies

**Use force-push cautiously**:
```shell
# Always verify what you're force-pushing
git log --graph --oneline --decorate --all

# Use --force-with-lease when possible (fails if remote has new commits)
git push --force-with-lease origin feature-branch
```

**Keep backups before major rebases**:
```shell
# Create backup branch before risky operations
git branch feature-branch-backup
git rebase -i origin/master
```

**Verify PR status after pushes**:
```shell
# After any force-push, verify PR is still open
gh pr view <PR_NUMBER> --json state,closed
```

## Anti-Patterns

**Wrong: Trying to fix the closed PR in-place**
```bash
# Don't waste time trying to reopen a corrupted PR
gh pr reopen <closed-pr>  # Often fails or creates confusion
git push --force origin <branch>  # May make things worse
```

**Wrong: Assuming work is lost**
```shell
# Commits are almost always safe on local branches
# Check local branch before considering any work lost
git log <feature-branch> --oneline -5
```

**Wrong: Creating a messy recovery**
- Multiple reopen attempts that confuse reviewers
- Force-pushing again to try to fix the original PR
- Not documenting what happened

**Right: Clean replacement PR**
- Acknowledge the accident quickly
- Create replacement from origin/master (see [clean-pr-creation.md](./clean-pr-creation.md))
- Document the transition clearly
- Move forward without dwelling

## Recovery Principles

1. **Speed over perfection**: A working replacement quickly beats a perfect recovery slowly
2. **Clean history**: Use the replacement as an opportunity to clean up commit history
3. **Clear documentation**: Make it easy for reviewers to understand what happened
4. **Forward momentum**: Don't let accidents derail progress

## Outcome

Following this pattern results in:
- **Zero work loss**: Commits are preserved on local/remote branches
- **Minimal disruption**: Quick recovery maintains work momentum
- **Clean history**: Replacement PR often has better commit structure than original
- **Clear communication**: Reviewers understand the transition
- **Reduced stress**: Structured recovery prevents panic and further mistakes

## Related
- [Clean PR Creation](./clean-pr-creation.md) - Creating branches from origin/master
- [Git Worktree Workflow](./git-worktree-workflow.md) - For complex PR management
- [When to Rebase PRs](./when-to-rebase-prs.md) - Safe rebase practices

## Origin
2026-02-09: Extracted from experience recovering from accidentally closing a PR during rebase.
Validated twice — pattern works consistently for force-push and branch-deletion recovery.
