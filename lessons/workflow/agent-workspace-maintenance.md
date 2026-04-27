---
match:
  keywords:
    - "update submodule to latest"
    - "submodule is behind"
status: active
---

# Agent Workspace Maintenance

## Rule
When doing periodic workspace maintenance, update the gptme-contrib submodule and verify that shared infrastructure symlinks are intact.

## Context
Applies when maintaining an existing agent workspace — running routine upkeep, noticing the submodule is behind, or verifying symlinks after a pull/merge. Does NOT apply to initial workspace creation (see `agent-workspace-setup-maintenance.md`).

## Detection
- gptme-contrib submodule is stale (`git submodule status` shows `+` or commit behind)
- Symlinks to gptme-contrib are broken or missing
- About to do periodic maintenance on an agent workspace
- Hooks or shared scripts are not behaving as expected

## Pattern

```bash
# 1. Update submodule to latest
git submodule update --remote gptme-contrib

# 2. Review what changed
git -C gptme-contrib log --oneline -10

# 3. Commit if there are changes (update may be a no-op if already at latest)
git add gptme-contrib
git diff --cached --quiet || git commit -m "chore: update gptme-contrib submodule"

# 4. Verify key symlinks still point to gptme-contrib
ls -la dotfiles/install.sh
ls -la dotfiles/.config/git/hooks
ls -la scripts/runs/autonomous/autonomous-loop.sh

# If a symlink is broken, recreate it:
# ln -sf ../gptme-contrib/dotfiles/install.sh dotfiles/install.sh
```

## Outcome
- Workspace stays aligned with latest shared infrastructure
- New lessons, scripts, and hooks from gptme-contrib become available
- Broken symlinks caught before they cause silent failures

## Related
- Initial setup: `lessons/workflow/agent-workspace-setup-maintenance.md`
- Full maintenance guide (symlink structure, troubleshooting): companion doc in agent brain repo
