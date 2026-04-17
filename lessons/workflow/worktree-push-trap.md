---
match:
  keywords:
    - "git worktree add"
    - "git push -u origin"
    - "branch from master"
    - "push.default=upstream"
status: active
---

# Worktree Push Trap: Accidentally Pushing Feature Commits to Master

## Rule
When creating a branch via `git worktree add -b BRANCH origin/master` (or any `git checkout -b BRANCH origin/master`), the branch's upstream becomes `origin/master`. Never use `git push -u origin BRANCH` afterward — it will push your commit **to the master branch on the remote**, not to a branch named BRANCH. Use an explicit refspec: `git push -u origin BRANCH:BRANCH`.

## Context
Applies whenever you create a feature branch from `origin/master` in a repo whose git config has `push.default=upstream`. Common in the worktree workflow where `git worktree add -b` is routine.

## Detection
- `git push -u origin <feature-branch>` output says `<feature-branch> -> master` (not `-> <feature-branch>`)
- A feature commit shows up on master without a PR
- `git config --get push.default` returns `upstream`
- `git config --get branch.<feature-branch>.merge` returns `refs/heads/master`

## Pattern
```bash
# ❌ Dangerous — branch tracks origin/master, push.default=upstream sends commit to master
git worktree add /tmp/worktrees/feat -b my-feature origin/master
cd /tmp/worktrees/feat
# ... make commits ...
git push -u origin my-feature   # pushes my-feature → MASTER on remote!

# ✅ Correct — explicit refspec forces the remote ref name
git push -u origin my-feature:my-feature
```

## Recovery
If the feature commit already landed on master:
- If the commit is good, leave it (clean linear history, no harm beyond bypassed review).
- If the commit needs reverting, open a PR with `git revert <sha>` rather than force-pushing.

## Outcome
Following this pattern:
- Feature commits land on feature branches, not master
- PR/review workflow preserved
- No "I pushed to master" recovery work

## Related
- [Branch From Master](./branch-from-master.md) — the pattern that sets up the trap
- [Git Worktree Workflow](./git-worktree-workflow.md)

## Prevention (repo-specific)
Some repos ship a pre-push hook that detects `local_ref != refs/heads/master` while `remote_ref == refs/heads/master` and blocks the push. Check for `scripts/git/pre-push-guard` or similar. If missing, the workflow rule above is the only defense.
