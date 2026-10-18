---
match:
  keywords:
    - "read ALL review sources without truncation"
    - "reply to individual review comment threads"
    - "batch all review replies in one shell block"
    - "maintainer says you didn't address review comments"
    - "unresolved review threads on PR"
status: active
description: "Always read ALL review sources without truncation using jq for compact output, and reply to individual review comment threads when acknowledging fixes."
---

# Read PR Reviews Comprehensively

## Rule
Read every page and the full body of discussion comments, review summaries, and
inline comments before acting. Reply in the relevant threads after verifying
fixes; post a completion summary only after those replies succeed.

## Context
When investigating or addressing pull-request feedback. This reading workflow
does not authorize posting or merging; follow the workspace's authorization and
coordination rules before writes.

## Detection
- Maintainer says feedback was missed despite reading `gh pr view --comments`.
- A read command uses `head`, slices `.body`, or omits REST pagination.
- A general summary claims completion without replies to actionable threads.
- Background replies use bare `wait`, hiding individual command failures.

## Pattern

### 1. Read all sources

Select useful fields to reduce metadata, keeping every body intact:

```bash
REPO="gptme/gptme-contrib"
PR_NUMBER=134

gh pr view "$PR_NUMBER" --repo "$REPO"

# General discussion (PRs use the issue-comments endpoint).
gh api --paginate "repos/$REPO/issues/$PR_NUMBER/comments?per_page=100" \
  --jq '.[] | {id, user: .user.login, body}'

# Review summaries and the commit each review assessed.
gh api --paginate "repos/$REPO/pulls/$PR_NUMBER/reviews?per_page=100" \
  --jq '.[] | {id, user: .user.login, state, commit_id, body}'

# Inline comments, including reply relationships and original locations.
gh api --paginate "repos/$REPO/pulls/$PR_NUMBER/comments?per_page=100" \
  --jq '.[] | {id, in_reply_to_id, path, line, original_line, user: .user.login, body}'
```

`per_page=100` alone is not pagination. Check each command's exit status; a failed
later page leaves an incomplete read. If the tool truncates the output, save it
to a file and read bounded sections until every comment is inspected. First-line
or three-line previews are indexes, never evidence that feedback was read.

REST inline comments do not report thread resolution state. When that matters,
query GraphQL `reviewThreads` with pagination and inspect `isResolved` separately.
A reply is not proof that a thread is resolved.

### 2. Act, then reply with evidence

Fix and verify the issues before composing replies. Re-read the thread before
posting so a concurrent session's reply is not duplicated. Reply to each
unaddressed actionable thread, with a commit reference or a reason for deferring.

For the reply endpoint, use the top-level review comment ID: the selected
comment's `in_reply_to_id` when present, otherwise its `id`. Replies to replies
are unsupported. These are REST numeric comment IDs, not GraphQL thread IDs.

Run writes sequentially with failure checking. For one thread and a prepared
summary file (repeat the reply command for additional threads before the summary):

```bash
(
  set -eu
  # Set these to the selected thread and an existing, reviewed summary file.
  : "${REPO:?}" "${PR_NUMBER:?}" "${TOP_LEVEL_COMMENT_ID:?}" "${SUMMARY_FILE:?}"
  gh api "repos/$REPO/pulls/$PR_NUMBER/comments/$TOP_LEVEL_COMMENT_ID/replies" \
    -f body='Fixed in commit abc123; the regression test now passes.' --jq '.id'
  gh pr comment "$PR_NUMBER" --repo "$REPO" --body-file "$SUMMARY_FILE"
)
```

A single tool call is not a transaction. Bare `wait` can return success when a
background reply failed, even with `set -e`. If a later write fails, earlier
replies remain posted: re-read before retrying and send only the missing replies.
Do not claim all feedback is addressed while requests remain deferred or open.

## Outcome
Full feedback survives compact formatting, long discussions are read completely,
and the summary accurately reflects successful thread replies and verified work.

## Related
- [Read Full GitHub Context](./read-full-github-context.md)
- [GitHub Issue Engagement](../social/github-issue-engagement.md)
- [GitHub CLI pagination](https://cli.github.com/manual/gh_api)
- [Review comment reply API](https://docs.github.com/en/rest/pulls/comments#create-a-reply-for-a-review-comment)

## Origin
2026-01-10, gptme/gptme-contrib#134: only a few review comments were read and a
general reply replaced thread-specific follow-through. The 2026-10-18 review
corrected body-truncating examples, missing pagination, and unchecked writes.
