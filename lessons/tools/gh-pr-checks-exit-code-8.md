---
match:
  keywords:
    - "exit code 8"
    - "gh pr checks watch pending"
    - "checks still pending"
    - "pr checks not complete"
status: active
generated_from:
  sessions: 538
  tool: bash
  error_signature: "Exit code 8"
---

# gh pr checks --watch Exits 8 for Pending Checks (Not a Failure)

## Rule
When `gh pr checks --watch` exits with code 8, it means checks are still in progress — treat it as "try again later", not as a failure.

## Context
When monitoring CI/CD status on pull requests using `gh pr checks --watch`. Exit code 8 signals checks are queued/pending, distinct from a real failure (exit code 1) or network error.

## Detection
Observable signals:
- `gh pr checks --watch` exits with code 8
- Output shows "pending", "queued", or "skipping" check statuses
- No "failed" or "error" check statuses in output

## Pattern
```bash
# Wrong: treating exit code 8 as failure
gh pr checks <PR> --watch || echo "Checks failed"

# Correct: distinguish pending from failure
gh pr checks <PR> --watch
exit_code=$?
if [ $exit_code -eq 8 ]; then
    echo "Checks still pending — wait and retry"
elif [ $exit_code -ne 0 ]; then
    echo "Checks failed with exit code $exit_code"
fi
```

## Outcome
- Avoids false-positive CI failure reports in journal/task updates
- Prevents premature "CI failed" diagnoses when checks are just queued
- Cleaner autonomous PR monitoring workflows

## Related
- [Greptile PR Reviews](./greptile-pr-reviews.md) - Other PR status monitoring patterns
