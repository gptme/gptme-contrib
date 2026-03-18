---
match:
  keywords:
  - "save file to"
  - "write journal entry"
  - "mkdir -p journal"
  - "file written to wrong"
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
# ❌ Wrong: relative path, CWD may have drifted to a different repo
# (e.g., after cd-ing into ~/gptme to apply a fix)
echo "..." >> journal/2025-10-14-topic.md  # creates in ~/gptme/journal/, not bob's!

# ✅ Correct: capture REPO_ROOT early, before CWD can change
REPO_ROOT=$(git rev-parse --show-toplevel)  # run this while CWD is in the correct repo
echo "..." >> "$REPO_ROOT/journal/2025-10-14-topic.md"
```

**Note**: `$(git rev-parse --show-toplevel)` returns the root of whichever repo contains the
current directory. Capture it at session start while CWD is known to be correct.

## Outcome
- **Reliability**: Works regardless of current directory
- **Prevents errors**: Files always go to intended location
- **No confusion**: Explicit about which repo/workspace

## Origin
Extracted from LOO effectiveness analysis in Bob's workspace (Δ=+0.198 session reward, p<0.001).
