---
match:
  keywords:
  - "save file to"
  - "write journal entry"
  - "mkdir -p journal"
  - "file path"
  - "wrong directory"
status: active
---

# Always Use Absolute Paths for Workspace Files

## Rule
Always use absolute paths when saving/appending to workspace files, especially journal entries.

## Context
When working across multiple repositories or when current directory might change during operation.

## Detection
- Files ending up in wrong directory
- Journal entries created in external repos
- "File not found" errors when appending
- Relative paths used with save/append tools

## Pattern
```bash
# ❌ Wrong: relative path, depends on cwd
cd ~/projects/gptme
echo "..." >> journal/2025-10-14-topic.md  # creates in wrong repo

# ✅ Correct: absolute path works from any directory
REPO_ROOT=$(git rev-parse --show-toplevel)
echo "..." >> "$REPO_ROOT/journal/2025-10-14-topic.md"
```

## Outcome
- **Reliability**: Works regardless of current directory
- **Prevents errors**: Files always go to intended location
- **No confusion**: Explicit about which repo/workspace

## Origin
Extracted from LOO effectiveness analysis in Bob's workspace (Δ=+0.198 session reward, p<0.001).
