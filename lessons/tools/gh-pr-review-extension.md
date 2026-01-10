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

### Step 3: Reply to threads + resolve (single toolcall!)
```shell
# Reply to threads using the extension (cleaner than gh api)
gh pr-review comments reply <pr> --thread-id "PRRT_thread1" --body "✅ Fixed in commit abc123"
gh pr-review comments reply <pr> --thread-id "PRRT_thread2" --body "✅ Fixed in commit abc123"
gh pr-review comments reply <pr> --thread-id "PRRT_thread3" --body "⚠️ Not addressing: low priority"

# Then resolve all threads
gh pr-review threads resolve <pr> --thread-id "PRRT_thread1"
gh pr-review threads resolve <pr> --thread-id "PRRT_thread2"
gh pr-review threads resolve <pr> --thread-id "PRRT_thread3"

# Verify all resolved
gh pr-review threads list <pr> --unresolved
# Should return empty []
```

**Why use `gh pr-review comments reply`?** Cleaner than raw `gh api` - the extension handles GraphQL thread IDs natively and produces minimal output.

**Key commands:**
- `threads list [pr] [--unresolved] [--mine]` - List review threads
- `threads resolve [pr] --thread-id <id>` - Mark thread as resolved
- `threads unresolve [pr] --thread-id <id>` - Reopen a thread
- `comments reply [pr] --thread-id <id> --body <text>` - Reply to a review thread

## Anti-Patterns
**Don't resolve without addressing:**
```shell
# ❌ WRONG: Resolve without fix or reply
gh pr-review threads resolve <pr> --thread-id "PRRT_xyz"

# ❌ WRONG: Using verbose gh api instead of extension
gh api repos/owner/repo/pulls/123/comments/<id>/replies -f body="Fixed"
# Returns ~30 fields of verbose output!
```

**Do this instead:**
```shell
# ✅ CORRECT: Fix, reply using extension, then resolve
git commit -m "fix: address review feedback"
git push

# Reply using the extension (cleaner, minimal output)
gh pr-review comments reply <pr> --thread-id "PRRT_xyz" --body "✅ Fixed"

# Then resolve
gh pr-review threads resolve <pr> --thread-id "PRRT_xyz"
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
2026-01-10 ErikBjare/bob#242: Erik introduced the gh-pr-review extension for resolving review comments/threads programmatically.
