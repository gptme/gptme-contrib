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

**Note:** For thread resolution status, GraphQL queries can filter unresolved threads (used by gptme's `gh pr view` tool internally). The REST API examples above are simpler and cover most use cases.

### Step 2: Acknowledge Each Thread + Post Summary (same toolcall!)
**Don't just post a general PR comment!** Reply to each review thread, THEN post summary - all in one shell block:

```shell
# Batch multiple replies in one shell block (efficient!)
# Use --jq to suppress verbose response output
gh api repos/$REPO/pulls/$PR_NUMBER/comments/2678822180/replies \
  -f body="✅ Fixed in commit 3389211" --jq '.id' &
gh api repos/$REPO/pulls/$PR_NUMBER/comments/2678822196/replies \
  -f body="⚠️ Not addressing: edge case, low priority" --jq '.id' &
gh api repos/$REPO/pulls/$PR_NUMBER/comments/2678822210/replies \
  -f body="✅ Fixed in commit 5ebce81" --jq '.id' &
wait

# Then post summary comment (same shell block - signals completion)
gh pr comment $PR_NUMBER --repo $REPO --body "## ✅ All Review Comments Addressed

Replied to all review threads individually. See thread replies for details.

- Issue A: Fixed in abc123
- Issue B: Not addressing (rationale)"
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

**❌ WRONG: Separate toolcalls for replies and summary**
```shell
# First toolcall - replies
gh api .../comments/123/replies -f body="Fixed"
```
```shell
# Second toolcall - summary (BAD: should be same block!)
gh pr comment 134 --body "All done"
```

**✅ CORRECT: Read → Act → Reply workflow**

The workflow has three distinct toolcall boundaries (NOT a single command sequence):

**Read**: Get all comments
```shell
gh api repos/$REPO/pulls/$PR_NUMBER/comments \
  --jq '.[] | {id, path, user: .user.login, body: (.body | split("\n")[0])}'
```

**Act**: Fix the issues (separate toolcalls)
This means using shell tool to make commits - may take multiple toolcalls across files.

**Reply**: Batch all replies + summary in one block
Only AFTER fixing issues, post replies and summary together:
```shell
gh api repos/$REPO/pulls/$PR_NUMBER/comments/<id1>/replies -f body="✅ Fixed in abc123" --jq '.id' &
gh api repos/$REPO/pulls/$PR_NUMBER/comments/<id2>/replies -f body="✅ Fixed in abc123" --jq '.id' &
wait

# Summary comment (plain text, not markdown table)
gh pr comment $PR_NUMBER --body "## All Review Comments Addressed

- Issue 1: Fixed in abc123
- Issue 2: Fixed in abc123
- Issue 3: Not addressing (rationale)"
```

**Why batch replies + summary?** Review comment thread replies have poor visibility - they're collapsed by default. The summary comment ensures the reviewer sees everything addressed. Keep Phase 3 in one toolcall to prevent partial completion.

## jq Quick Reference for PR Reviews

```text
Compact comment list:
  --jq '.[] | {id, path, user: .user.login, body}'

First line of body only:
  --jq '.[] | {id, body: (.body | split("\n")[0])}'

Suppress POST response:
  --jq '.id'

Count comments:
  --jq 'length'

Filter by user:
  --jq '.[] | select(.user.login == "ErikBjare")'

Unresolved threads only:
  GraphQL with: select(.isResolved == false)
```

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
