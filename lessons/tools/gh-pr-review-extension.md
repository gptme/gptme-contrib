---
match:
  keywords:
    - "resolve review thread"
    - "resolve review comment"
    - "review threads"
    - "gh-pr-review"
    - "close conversation thread"
    - "mark thread resolved"
---

# gh-pr-review Extension for Review Thread Management

## Rule
Use the `gh-pr-review` extension to list and resolve PR review threads after addressing feedback.

## Context
When working on PRs with review comments that need to be resolved after implementing fixes.

## Installation
```shell
gh extension install agynio/gh-pr-review
```

## Detection
Observable signals you need this tool:
- PR has review threads that need to be resolved
- Want to signal to reviewers that feedback was addressed
- Need to list unresolved review threads
- Managing multiple review comment threads

## Pattern
Complete workflow for addressing review feedback efficiently:

### Step 1: List unresolved threads (compact output)
```shell
# Filter to unresolved only
gh pr-review threads list <pr> --unresolved
# Output: [{"threadId":"PRRT_xyz...","isResolved":false,"path":"file.py","line":50}]
```

### Step 2: Fix issues and commit
```shell
# Make code changes, then commit
git commit -m "fix: address review feedback"
git push
```

### Step 3: Reply to threads in parallel + resolve (single toolcall!)
```shell
# Batch all replies with --jq to suppress verbose output
gh api repos/owner/repo/pulls/123/comments/<id1>/replies \
  -f body="✅ Fixed in commit abc123" --jq '.id' &
gh api repos/owner/repo/pulls/123/comments/<id2>/replies \
  -f body="✅ Fixed in commit abc123" --jq '.id' &
gh api repos/owner/repo/pulls/123/comments/<id3>/replies \
  -f body="⚠️ Not addressing: low priority" --jq '.id' &
wait

# Then resolve all threads
gh pr-review threads resolve <pr> --thread-id "PRRT_thread1"
gh pr-review threads resolve <pr> --thread-id "PRRT_thread2"
gh pr-review threads resolve <pr> --thread-id "PRRT_thread3"

# Verify all resolved
gh pr-review threads list <pr> --unresolved
# Should return empty []
```

**Why `--jq '.id'`?** Raw `gh api` POST responses include ~30 fields. The jq filter returns only the comment ID, reducing context usage by ~90%.

**Why parallel with `&` and `wait`?** Posting replies sequentially wastes time. Parallel execution is faster and keeps all replies in one toolcall.

**Key commands:**
- `threads list [pr] [--unresolved] [--mine]` - List review threads
- `threads resolve [pr] --thread-id <id>` - Mark thread as resolved
- `threads unresolve [pr] --thread-id <id>` - Reopen a thread

## Anti-Patterns
**Don't resolve without addressing:**
```shell
# ❌ WRONG: Resolve without fix or reply
gh pr-review threads resolve ... --thread-id "PRRT_xyz"

# ❌ WRONG: Verbose API output polluting context
gh api .../comments/<id>/replies -f body="Fixed"  # Returns ~30 fields!

# ❌ WRONG: Sequential replies (slow, multiple toolcalls)
gh api .../comments/<id1>/replies -f body="Fixed"
gh api .../comments/<id2>/replies -f body="Fixed"  # Separate toolcall
```

**Do this instead:**
```shell
# ✅ CORRECT: Fix, reply in parallel with jq, then resolve
git commit -m "fix: address review feedback"
git push

# Batch parallel replies with suppressed output
gh api .../comments/<id1>/replies -f body="✅ Fixed" --jq '.id' &
gh api .../comments/<id2>/replies -f body="✅ Fixed" --jq '.id' &
wait

# Then resolve
gh pr-review threads resolve ... --thread-id "PRRT_xyz"
```

## Outcome
Following this pattern results in:
- **Context efficiency**: `--jq '.id'` reduces output by ~90%
- **Faster workflow**: Parallel replies with `&` and `wait`
- **Clear communication**: Reviewers see threads addressed and resolved
- **Professional workflow**: Single toolcall for all replies

## Related
- [Read PR Reviews Comprehensively](../workflow/read-pr-reviews-comprehensively.md)
- Blog post: https://agyn.io/blog/gh-pr-review-cli-agent-workflows

## Origin
2026-01-10 Issue #242: Erik introduced the gh-pr-review extension for resolving review comments/threads programmatically.
