---
match:
  keywords:
  - gh issue view
  - gh pr view
  - missing critical discussions in comments
  - duplicate work coordination
  - responding without full context
  - only seeing initial description
  - responding to old comments
  - not reading whole thread
  - replying to stale context
---

# Read Full GitHub Context

## Rule
ALWAYS read both basic view AND comments view, then read the ENTIRE thread chronologically before responding.

## Context
Whenever reading, investigating, or working with GitHub issues or pull requests in any repository.

## Detection
Observable signals of incomplete context reading:
- Missing critical discussions that happened in comments
- Duplicate work because you didn't see coordination in comments
- Incomplete understanding of issue/PR status
- Missing maintainer feedback or decisions
- Responding to issues without full context
- Only seeing initial description, missing follow-up clarifications
- **Responding to old comments without noticing newer replies**
- **Replying to stale context instead of current thread state**
- Maintainer saying "You're not reading the whole issue"

## Pattern
Read BOTH views every time - basic and comments:
```shell
# For issues: ALWAYS run both commands
gh issue view <number>           # Basic view
gh issue view <number> --comments # Full discussion

# For PRs: ALWAYS run complete sequence
gh pr view <pr-url>              # Basic PR info
gh pr view <pr-url> --comments   # Discussion comments
gh api repos/<owner>/<repo>/pulls/<pr-number>/reviews \
  | jq '.[] | {user: .user.login, state: .state}'
gh api repos/<owner>/<repo>/pulls/<pr-number>/comments \
  | jq '.[] | {path: .path, line: .line}'
```

## Anti-Pattern: Responding to Stale Context

A common failure mode is reading comments but responding to OLD comments without noticing NEWER replies that change the context.

**What happens:**
1. You read an issue and see a comment from yesterday
2. You reply to that comment
3. BUT there were newer comments TODAY that you missed or didn't fully process
4. Your response is to stale context, not the current thread state

**Example (from Issue #290):**
```text
Comment 1 (old): "How does system X work?"
Comment 2 (old): Agent explains system X
Comment 3 (new): User asks about system Y
Comment 4 (new): User clarifies they meant system Y, not X
Comment 5: Agent responds about system X again  ← WRONG: missed newer context!
```

**Prevention:** After reading `--comments`, mentally trace the conversation flow:
1. What was originally asked?
2. How did the conversation evolve?
3. What is the MOST RECENT question/request?
4. Only THEN respond to the current state

## Correct Reading Protocol

```shell
# 1. Get the full picture
gh issue view <number> --comments

# 2. Before responding, answer these questions:
#    - What is the LATEST comment asking for?
#    - Did any earlier questions get answered/superseded?
#    - Is there ongoing discussion I should acknowledge?

# 3. Respond to CURRENT context, not stale threads
gh issue comment <number> --body "Responding to your latest question about..."
```

## Outcome
Following this pattern leads to:
- **Complete context**: See all discussions, decisions, and coordination
- **Better responses**: Understand full history before commenting
- **Avoid duplication**: Know if others are already working on it
- **Maintainer respect**: Shows you've read the discussion
- **Quality work**: Complete information prevents mistakes
- **Current responses**: Reply to latest context, not stale threads

Benefits in autonomous runs:
- No missing critical feedback
- Better decision-making with full context
- Proper coordination with other contributors
- No "you're not reading the whole issue" feedback

## Related
- [GitHub Issue Engagement](../social/github-issue-engagement.md) - Issue handling best practices
- [Memory Failure Prevention](./memory-failure-prevention.md) - Preventing context loss across sessions
