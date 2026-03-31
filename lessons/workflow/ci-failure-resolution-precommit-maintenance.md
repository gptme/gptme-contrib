---
status: active
match:
  keywords:
    - "multiple consecutive CI failures on master"
    - "restore corrupted pre-commit scripts"
    - "prek run failing with persistent errors"
    - "CI status red on master branch"
---

# CI Failure Resolution and Pre-commit Infrastructure Maintenance

## Rule
When CI is red on master or pre-commit scripts are corrupted, diagnose systematically before fixing: check failure pattern, run locally, fix root cause, verify.

## Context
When pre-commit CI failures block commits or indicate infrastructure degradation. Not for normal pre-commit runs that flag code issues — those are expected workflow.

## Detection
- Pre-commit workflow runs failing repeatedly on master (not just a single commit)
- `prek run --all-files` fails on a clean checkout
- Corrupted or missing pre-commit scripts after a bad merge/rebase
- Multiple agents failing on the same hook

## Pattern
```bash
# 1. Diagnose locally
prek run --all-files

# 2. Fix common issues
make format          # formatting
make typecheck       # typing

# 3. Restore corrupted scripts from last good commit
git log --oneline -- scripts/precommit/
git show <commit>:scripts/precommit/script.py > scripts/precommit/script.py

# 4. If hooks keep failing after fixes
prek clean           # clear cache
prek install --force # reinstall hooks

# 5. Verify all clean
prek run --all-files
```

## Outcome
- Master branch stays green
- No blocking CI issues for subsequent sessions
- Infrastructure integrity maintained

## Related
- [Git Workflow](./git-workflow.md) — Commit practices
