---
match:
  keywords:
    - "pathspec did not match"
    - "nothing added to commit"
    - "Stashing unstaged"
    - "prek shows old errors"
    - "commit untracked file"
status: active
---

# Stage Files with git add Before Commit/prek

## Rule
Always `git add <files>` before committing or running prek. New files must be staged first; prek validates the staged version, not your working directory.

## Context
Git distinguishes tracked vs untracked files. `git commit FILE` only works for tracked files. prek stashes unstaged changes, so fixes aren't validated unless staged.

## Detection
- Error: 'pathspec... did not match any file(s) known to git'
- prek shows 'Unstaged files detected' or 'Stashing unstaged files'
- prek reports same errors after you've already fixed them
- File appears under 'Untracked files' in `git status`

## Pattern
```bash
# ❌ Wrong: Commit untracked file directly
git commit new-file.md -m "add new file"
# Error: pathspec 'new-file.md' did not match any file(s) known to git

# ❌ Wrong: Run prek without staging fixes
vim scripts/fix.py  # fix issues
prek run       # still shows old errors

# ✅ Correct: Stage first, then commit/prek
git add new-file.md && git commit new-file.md -m "add new file"
git add scripts/fix.py && prek run
```

## Outcome
- New files commit successfully without pathspec errors
- Pre-commit validates your actual fixes, not stale staged versions
- Eliminates confusion from seeing errors on already-fixed code

## Related
- [Git Workflow](../workflow/git-workflow.md) - Complete git workflow
