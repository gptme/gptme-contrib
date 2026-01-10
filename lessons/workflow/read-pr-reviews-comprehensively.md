---
match:
  keywords: ["gh pr view", "pull request", "pr review", "review comments", "inline comments", "gh api"]
---

# Read PR Reviews Comprehensively

## Rule
Always read ALL review sources without truncation using jq for compact output, and reply to individual review comment threads when acknowledging fixes.

## Context
When investigating, responding to, or working on pull requests in any GitHub repository.

## Detection
Observable signals you need comprehensive PR review reading:
- Maintainer saying "You didn't address the comments I left in my review"
- Missing inline review comments that aren't shown in basic PR view
- Only reading `gh pr view --comments` but missing review-specific feedback
- **Using `| head -n 50` or similar truncation on review output**
- Reading only 3-5 comments when there are 15+ review comments
- Posting a general PR comment instead of replying to specific threads
- Context bloat from verbose `gh api` output

## Pattern
**CRITICAL: Never truncate review output!** Use jq for compact, context-efficient output.

### Step 1: Read ALL Review Sources (with jq for efficiency)
```shell
PR_NUMBER=134
REPO="gptme/gptme-contrib"

# Basic info
gh pr view $PR_NUMBER --repo $REPO

# General comments (don't use | head!)
gh pr view $PR_NUMBER --repo $REPO --comments

# All reviews (compact with jq)
gh api repos/$REPO/pulls/$PR_NUMBER/reviews \
  --jq '.[] | {user: .user.login, state: .state, body: .body}'

# ALL inline review comments (compact with jq - context efficient!)
gh api repos/$REPO/pulls/$PR_NUMBER/comments \
  --jq '.[] | {id: .id, path: .path, user: .user.login, body: (.body | split("\n")[0:3] | join(" "))}'
```

**Why jq?** Raw `gh api` output includes ~50 fields per comment (timestamps, URLs, reactions, etc.). The jq filter extracts only what's needed, reducing context usage by ~80% while preserving actionable information.

### Step 2: Acknowledge Each Thread Individually (suppress output with jq)
**Don't just post a general PR comment!** Reply to each review comment thread:

```shell
# Reply to individual review comment threads (signals ready to resolve)
# Use --jq to suppress verbose response output
gh api repos/$REPO/pulls/$PR_NUMBER/comments/<comment_id>/replies \
  -f body="✅ Fixed in commit abc123" \
  --jq '.id' 2>/dev/null  # Returns only the new comment ID

# Batch multiple replies in one shell block (efficient!):
gh api repos/$REPO/pulls/$PR_NUMBER/comments/2678822180/replies \
  -f body="✅ Fixed in commit 3389211" --jq '.id' &
gh api repos/$REPO/pulls/$PR_NUMBER/comments/2678822196/replies \
  -f body="⚠️ Not addressing: edge case, low priority" --jq '.id' &
gh api repos/$REPO/pulls/$PR_NUMBER/comments/2678822210/replies \
  -f body="✅ Fixed in commit 5ebce81" --jq '.id' &
wait
echo "All replies posted"
```

### Step 3: Post Summary Comment (optional)
After individual thread replies, optionally post a summary:
```shell
gh pr comment $PR_NUMBER --repo $REPO --body "## ✅ All Review Comments Addressed

Replied to all review threads individually. See thread replies for details."
```

## Anti-Patterns

**❌ WRONG: Truncating output**
```shell
gh pr view 134 --comments | head -50  # Misses most comments!
gh api .../comments | head -100       # Truncates review feedback!
```

**❌ WRONG: Raw gh api output (context bloat)**
```shell
gh api repos/$REPO/pulls/$PR_NUMBER/comments  # 50+ fields per comment!
```

**❌ WRONG: General PR comment only**
```shell
# This doesn't close individual review threads
gh pr comment 134 --body "Fixed all issues"
```

**✅ CORRECT: jq-filtered output + individual thread replies**
```shell
# Read ALL comments with compact output
gh api repos/$REPO/pulls/$PR_NUMBER/comments \
  --jq '.[] | {id, path, user: .user.login, body: (.body | split("\n")[0])}'

# Reply to EACH thread with suppressed output
gh api .../comments/<id1>/replies -f body="✅ Fixed" --jq '.id'
gh api .../comments/<id2>/replies -f body="✅ Fixed" --jq '.id'
```

## jq Quick Reference for PR Reviews

| Use Case | jq Filter |
|----------|-----------|
| Compact comment list | `--jq '.[] \| {id, path, user: .user.login, body}'` |
| First line of body only | `--jq '.[] \| {id, body: (.body \| split("\n")[0])}'` |
| Suppress POST response | `--jq '.id'` |
| Count comments | `--jq 'length'` |
| Filter by user | `--jq '.[] \| select(.user.login == "ErikBjare")'` |

## Outcome
Following this pattern results in:
- **Complete feedback**: See ALL review comments, not just first few
- **Context efficiency**: jq filters reduce token usage by ~80%
- **Professional response**: Address everything maintainer requested
- **Thread closure**: Individual replies signal threads ready to resolve
- **No re-reviews needed**: Get it right the first time

Benefits:
- Catch ALL inline comments (not just first 3-5)
- Signal to reviewer which threads are addressed
- Prevent context bloat from verbose API output
- Clear audit trail of what was fixed

## Related
- [Read Full GitHub Context](./read-full-github-context.md) - Issues and basic PR comments
- [GitHub Issue Engagement](../social/github-issue-engagement.md) - Issue handling

## Origin
2026-01-10 PR #134: Identified failure mode where agent only read ~3 review comments due to output truncation and posted general PR comment instead of replying to individual review threads. Erik requested upstreaming with jq patterns for context efficiency.
