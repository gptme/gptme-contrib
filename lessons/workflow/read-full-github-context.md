---
match:
  keywords:
  - gh issue view
  - gh pr view
  - missing critical discussions in comments
  - duplicate work coordination
  - responding without full context
  - only seeing initial description
---

# Read Full GitHub Context

## Rule
ALWAYS read both basic view AND comments view when checking any GitHub issue or PR.

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

## Pattern
Read BOTH views every time - basic and comments:
```shell
# For issues: ALWAYS run both commands
gh issue view <number>           # Basic view
gh issue view <number> --comments # Full discussion

# For PRs: Read ALL sources (basic + comments + reviews + inline)
gh pr view <pr-url>              # Basic PR info
gh pr view <pr-url> --comments   # Discussion comments

# CRITICAL: Also read review comments (often missed!)
gh api repos/<owner>/<repo>/pulls/<pr-number>/reviews \
  --jq '.[] | {user: .user.login, state: .state, body: .body}'
gh api repos/<owner>/<repo>/pulls/<pr-number>/comments \
  --jq '.[] | {id: .id, path: .path, body: (.body | split("\n")[0])}'
```

**Note**: For comprehensive PR review handling including thread replies, see [Read PR Reviews Comprehensively](./read-pr-reviews-comprehensively.md).

## Outcome
Following this pattern leads to:
- **Complete context**: See all discussions, decisions, and coordination
- **Better responses**: Understand full history before commenting
- **Avoid duplication**: Know if others are already working on it
- **Maintainer respect**: Shows you've read the discussion
- **Quality work**: Complete information prevents mistakes

Benefits in autonomous runs:
- No missing critical feedback
- Better decision-making with full context
- Proper coordination with other contributors

## Related
- [GitHub Issue Engagement](../social/github-issue-engagement.md) - Issue handling best practices
