---
match:
  keywords:
    - "gh issue view"
    - "gh pr view"
    - "--comments"
    - "| head"
    - "| tail"
    - "issue thread"
---

# Read Full GitHub Context

## Rule
NEVER truncate GitHub comment output. Read the ENTIRE thread chronologically before responding.

## Context
Whenever reading GitHub issues or PRs via `gh` CLI, especially with `--comments` flag.

## Detection
Observable signals of truncation:
- Using `gh pr view --comments | head` or `| tail`
- Missing newer comments that supersede earlier ones
- Maintainer saying "You're not reading the whole issue"
- Responding to stale context when conversation evolved

## Pattern
Read full output, no truncation:
```shell
# Issues: ALWAYS both views, no truncation
gh issue view <number>
gh issue view <number> --comments

# PRs: Full sequence, no truncation
gh pr view <pr-url>
gh pr view <pr-url> --comments
```

**After reading**: Trace conversation chronologically. What is the LATEST request? Respond to current state, not old comments.

## Anti-Pattern

```shell
# ❌ WRONG: Truncating loses newer context
gh issue view 123 --comments | head -50
gh pr view 456 --comments | tail -20

# ✅ CORRECT: Read everything
gh issue view 123 --comments
```

Truncation causes responding to OLD comments while missing NEWER replies that changed context.

## Outcome
- **Complete context**: See all discussions chronologically
- **Current responses**: Reply to latest state, not stale threads
- **No re-asks**: Maintainers don't need to repeat themselves

## Related
- [Read PR Reviews Comprehensively](./read-pr-reviews-comprehensively.md) - PR-specific patterns
