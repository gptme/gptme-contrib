---
match:
  keywords:
  - "github issue"
  - "open issue"
  - "create issue"
  - "close issue"
  - "about to create new issue"
  - "duplicate issue prevention"
  - "search existing issues first"
  - "check for similar issues"
  - "update issue after work"
  - "gh issue"
  - "check own previous comments"
  - "agent created duplicate issues"
status: active
---

# GitHub Issue and PR Engagement

## Rule
Always search for existing issues/PRs before creating new ones, read full context before engaging, and update issues/PRs after completing work.

## Context
When planning to work on a feature, fix, or improvement in any GitHub repository, and after completing work in response to comments.

## Detection
Observable signals indicating this need:

**Pre-work**:
- About to create new issue without searching first
- Starting work without checking if someone else is addressing it
- Reading issue/PR basic view without checking comments
- Creating PR without checking for existing work
- Missing ongoing discussions about the same topic
- **Returning to a thread without checking own previous comments**

**Post-work**:
- Completed work in response to issue/PR comment
- Fixed something requested by maintainer
- About to move on without updating the issue/PR
- Risk of confusion in future autonomous runs about what's done

## Pattern
Check, read full context, coordinate, work, then update:

```shell
# 0. Pre-flight: Check own previous actions (CRITICAL for agents)
gh issue view <number> --comments | grep -A5 "$(gh api user --jq .login)"
# If you already commented/acted → DON'T duplicate. Update existing if needed.

# 1. Search for existing work
gh issue list --repo owner/repo --search "topic keywords"
gh pr list --repo owner/repo --search "related terms"

# 2. If found: Read BOTH views (critical)
gh issue view <number>           # Basic view
gh issue view <number> --comments  # Full discussion

# For PRs: Also check reviews and inline comments
gh pr view <pr-url>
gh pr view <pr-url> --comments
gh api repos/<owner>/<repo>/pulls/<pr-number>/reviews
gh api repos/<owner>/<repo>/pulls/<pr-number>/comments

# 3. Coordinate before starting work
gh issue comment <number> --body "I'd like to help with this"

# 4. If not found: Create issue first, wait for feedback
gh issue create --title "..." --body "..."

# 5. After completing work: Update with status IN THE ORIGINATING THREAD
gh issue comment <number> --body "✅ Completed: [brief summary]

Details:
- [What was done]
- [Relevant commits/PRs]
- [Any remaining work]"
```

## Outcome
Following this pattern results in:
- No duplicate work (saves hours of wasted effort)
- Better solutions (builds on others' insights)
- Community respect (shows consideration for workflow)
- Complete context (all discussions considered)
- Coordinated effort (no stepping on toes)
- **Clear status tracking** (maintainers know what's done)
- **No confusion in future runs** (autonomous agents see completed work)
- **Professional workflow** (closes communication loop)

## Related
- [Read Full GitHub Context](../workflow/read-full-github-context.md) - Why both views matter
- [Check Existing PRs](../workflow/check-existing-prs.md) - PR-specific workflow
