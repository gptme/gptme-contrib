---
match:
  keywords:
  - "save file to"
  - "write journal entry"
  - "mkdir -p journal"
  - "file path"
  - "wrong directory"
  - "file ended up in wrong"
  - "journal created in external repo"
status: active
---

# Always Use Absolute Paths for Workspace Files

## Rule
Always use absolute paths when saving/appending to workspace files. Never use relative paths or `git rev-parse --show-toplevel` — both break when the current directory is a different repository.

## Context
When working across multiple repositories. If you `cd` into an external repo (e.g., a PR worktree), relative paths and `git rev-parse --show-toplevel` resolve to that repo, not your workspace.

## Detection
- Files ending up in wrong directory (e.g., journal entry created in external repo)
- "File not found" errors when appending to a previously written file
- Relative paths used with save/append tools
- `git rev-parse --show-toplevel` returns external repo path instead of workspace

## Pattern
```bash
# ❌ Wrong: relative path, depends on cwd
echo "..." >> journal/2025-10-14/session.md  # silently writes to current repo!

# ❌ Wrong: git rev-parse resolves to *current* git root, not workspace
REPO_ROOT=$(git rev-parse --show-toplevel)
echo "..." >> "$REPO_ROOT/journal/..."  # silently writes to external repo — no error, data goes to wrong location

# ✅ Correct: hardcoded absolute path always works
echo "..." >> /home/agent/workspace/journal/2025-10-14/session.md

# ✅ Correct: use WORKSPACE env var set at session start
# Guard against unset: if WORKSPACE is empty, paths expand to /journal/... (wrong!)
: "${WORKSPACE:?WORKSPACE is not set — export it at session start: export WORKSPACE=/home/agent/workspace}"
echo "..." >> "$WORKSPACE/journal/2025-10-14/session.md"
```

For write/save tools, always provide the full absolute path:
```text
# ❌ Wrong
save journal/2025-10-14/session.md

# ✅ Correct
save /home/agent/workspace/journal/2025-10-14/session.md
```

## Outcome
- **Reliability**: Works regardless of current working directory
- **Prevents data loss**: Files always go to the intended location
- **No confusion**: Explicit about which repo/workspace receives the file

## Related
- [Git Worktree Workflow](./git-worktree-workflow.md) - Working in external repos
