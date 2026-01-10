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
Complete workflow for addressing review feedback:

```shell
# 1. List unresolved threads on a PR
gh pr-review threads list <pr-url-or-number>
gh pr-review threads list https://github.com/owner/repo/pull/123
gh pr-review threads list 123 --repo owner/repo

# Output shows threadId, isResolved, path, line, isOutdated
# [{"threadId":"PRRT_xyz...","isResolved":false,"path":"file.py","line":50}]

# 2. Filter to unresolved only
gh pr-review threads list <pr> --unresolved

# 3. After implementing fix, reply to comment thread first
gh api repos/<owner>/<repo>/pulls/<pr>/comments/<comment_id>/replies \
  -f body="✅ Fixed in commit abc123"

# 4. Then resolve the thread
gh pr-review threads resolve <pr-url> --thread-id "PRRT_xyz..."

# 5. Verify all threads resolved
gh pr-review threads list <pr> --unresolved
# Should return empty []
```

**Key commands:**
- `threads list [pr] [--unresolved] [--mine]` - List review threads
- `threads resolve [pr] --thread-id <id>` - Mark thread as resolved
- `threads unresolve [pr] --thread-id <id>` - Reopen a thread
- `comments` - Reply to review threads
- `review` - Manage pending reviews

## Anti-Pattern
**Don't resolve without addressing the feedback:**
```shell
# ❌ WRONG: Resolve without fix or reply
gh pr-review threads resolve ... --thread-id "PRRT_xyz"

# ✅ CORRECT: Fix, reply, then resolve
git commit -m "fix: address review feedback"
git push
gh api .../comments/<id>/replies -f body="✅ Fixed in commit abc123"
gh pr-review threads resolve ... --thread-id "PRRT_xyz"
```

## Outcome
Following this pattern results in:
- **Clear communication**: Reviewers see threads addressed and resolved
- **Professional workflow**: Shows attention to feedback
- **Efficient reviews**: Resolved threads collapse in GitHub UI
- **Progress tracking**: Easy to see what's still outstanding

## Related
- [Read PR Reviews Comprehensively](../workflow/read-pr-reviews-comprehensively.md)
- Blog post: https://agyn.io/blog/gh-pr-review-cli-agent-workflows

## Origin
2026-01-10 Issue #242: Erik introduced the gh-pr-review extension for resolving review comments/threads programmatically.
