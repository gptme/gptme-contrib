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

# For PRs: ALWAYS run complete sequence
gh pr view <pr-url>              # Basic PR info
gh pr view <pr-url> --comments   # Discussion comments
gh api repos/<owner>/<repo>/pulls/<pr-number>/reviews \
  | jq '.[] | {user: .user.login, state: .state}'
gh api repos/<owner>/<repo>/pulls/<pr-number>/comments \
  | jq '.[] | {path: .path, line: .line}'
```

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
