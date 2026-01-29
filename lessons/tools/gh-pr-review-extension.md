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
Use the `gh-pr-review` extension to manage PR review threads: **comment + resolve** when the issue is resolved (pushed fix OR explained why it's not an issue), **comment only** when seeking clarification or deferring.

## Context
When working on PRs with review comments that need responses or fixes.

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
# Using PR URL (auto-detects repo)
gh pr-review threads list https://github.com/owner/repo/pull/123 --unresolved

# Or using PR number (requires --repo)
gh pr-review threads list --repo owner/repo 123 --unresolved
# Output: [{"threadId":"PRRT_xyz...","isResolved":false,"path":"file.py","line":50}]
```

**Note**: When using numeric PR selectors, `--repo owner/repo` is required. PR URLs work without it.

### Step 2: Fix issues and commit
```shell
# Make code changes, then commit
git commit -m "fix: address review feedback"
git push
```

### Step 3: Reply to threads + resolve (single toolcall!)
```shell
# Reply to threads using the extension (cleaner than gh api)
# Resolve the threads with pushed fixes
# Use --repo owner/repo with numeric PRs, or use PR URLs directly
gh pr-review comments reply --repo owner/repo 123 --thread-id "PRRT_thread1" --body "✅ Fixed in commit abc123"
gh pr-review threads resolve --repo owner/repo 123 --thread-id "PRRT_thread1"
gh pr-review comments reply --repo owner/repo 123 --thread-id "PRRT_thread2" --body "This is intentional - the API expects nullable here for backwards compat"
gh pr-review threads resolve --repo owner/repo 123 --thread-id "PRRT_thread2"
# Thread2 resolved: no code change, but explained why current behavior is correct

# If you need clarification, comment only (no resolve):
gh pr-review comments reply --repo owner/repo 123 --thread-id "PRRT_thread3" --body "Could you share an example of where this would cause issues?"
# Thread3 stays open - waiting for reviewer response

# Verify
gh pr-review threads list --repo owner/repo 123 --unresolved
# Should return only thread3 (pending clarification)
```

**Why use `gh pr-review comments reply`?** Cleaner than raw `gh api` - the extension handles GraphQL thread IDs natively and produces minimal output.

**Key commands:**
- `threads list [pr] [--unresolved] [--mine]` - List review threads
- `threads resolve [pr] --thread-id <id>` - Mark thread as resolved
- `threads unresolve [pr] --thread-id <id>` - Reopen a thread
- `comments reply [pr] --thread-id <id> --body <text>` - Reply to a review thread

## Anti-Patterns
**Don't resolve without a fix:**
```shell
# ❌ WRONG: Resolve without pushing a fix
gh pr-review comments reply <pr> --thread-id "PRRT_xyz" --body "Good point, will consider"
gh pr-review threads resolve <pr> --thread-id "PRRT_xyz"  # NO! Didn't fix anything

# ❌ WRONG: Resolve without any reply
gh pr-review threads resolve <pr> --thread-id "PRRT_xyz"
```

**Correct patterns:**
```shell
# ✅ CORRECT: Pushed fix → comment + resolve
git commit -m "fix: address review feedback"
git push
gh pr-review comments reply <pr> --thread-id "PRRT_xyz" --body "✅ Fixed in abc123"
gh pr-review threads resolve <pr> --thread-id "PRRT_xyz"

# ✅ CORRECT: Explained as non-issue → comment + resolve
gh pr-review comments reply <pr> --thread-id "PRRT_abc" --body "This is intentional for backwards compat, see design doc"
gh pr-review threads resolve <pr> --thread-id "PRRT_abc"

# ✅ CORRECT: Need clarification → comment only, NO resolve
gh pr-review comments reply <pr> --thread-id "PRRT_def" --body "Could you elaborate on the expected behavior here?"
# Thread stays open - waiting for response
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
