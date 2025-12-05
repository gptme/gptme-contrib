---
match:
  keywords: ["@greptileai", "greptile", "PR review", "code quality", "Codecov"]
---

# Triggering Greptile PR Reviews

## Rule
Use "@greptileai review" comment to trigger fresh code quality reviews on PRs after making improvements.

## Context
When working on PRs that:
- Previously received low code quality scores from Greptile
- Have been improved with bug fixes or additional tests
- Need validation that quality improvements were effective
- Are ready for re-review after addressing feedback

## Detection
Observable signals that you need to trigger Greptile review:
- PR has existing Greptile review with low score (e.g., 3/5)
- Made improvements (fixed bugs, added tests, improved coverage)
- Want to verify improvements meet quality standards before requesting human review
- Codecov shows improved coverage but no new Greptile review

Common scenario:
```text
1. PR receives Greptile review: 3/5 (critical bugs found)
2. You fix the bugs and add comprehensive tests
3. Need to verify the fixes improved quality
4. Trigger new review with "@greptileai review"
```

## Pattern
Trigger review with comment:
```shell
# After making improvements to PR
gh pr comment <pr-url> --body "@greptileai review"

# Wait 5-10 minutes for Greptile to analyze
# Check PR for new review comment

# Example:
gh pr comment https://github.com/gptme/gptme/pull/841 --body "@greptileai review"
```

**Note**: Greptile should auto-review new PRs, but manual trigger is useful for:
- Re-review after improvements
- Validating quality before requesting human review
- Ensuring fixes addressed previous issues

## Outcome
Following this pattern results in:
- **Quality validation**: Confirms improvements are effective
- **Professional workflow**: Shows attention to code quality
- **Faster human review**: Pre-validated PRs get approved faster
- **Learning**: Understand what "good" looks like per Greptile's analysis

Benefits:
- 5-10 minute turnaround for quality check
- Specific feedback on remaining issues
- Score improvement visible (3/5 → 5/5)
- Catches issues before human reviewers see them
