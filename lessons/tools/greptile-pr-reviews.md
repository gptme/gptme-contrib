---
match:
  keywords:
    - "trigger greptile re-review after improvements"
    - "PR received low quality score from greptile"
    - "validate quality improvements before human review"
    - "@greptileai review comment"
    - "@greptileai"
    - "greptile review"
    - "greptile PR"
    - "request greptile review"
status: active
---

# Triggering Greptile PR Reviews

## Rule
Never post raw `@greptileai review` comments directly. Use `greptile-helper.sh` for all Greptile triggers.

## Context
When working on PRs that:
- Previously received low code quality scores from Greptile
- Have been improved with bug fixes or additional tests
- Need validation that quality improvements were effective
- Are ready for re-review after addressing feedback

## Detection
Observable signals that indicate you need a Greptile re-review:
- PR has an existing Greptile review with a low score (for example 3/5)
- You made improvements (fixed bugs, added tests, improved coverage)
- You want to verify the improvements before requesting human review
- Coverage or test results improved, but there is no new Greptile review yet

## Pattern
Always route re-reviews through the helper:
```shell
# Trigger safely
bash scripts/github/greptile-helper.sh trigger OWNER/REPO PR_NUMBER

# Or inspect state first
bash scripts/github/greptile-helper.sh status OWNER/REPO PR_NUMBER
# Returns: already-reviewed | needs-re-review | in-progress | awaiting-initial-review | stale | error
```

Do not use raw comments:
```shell
# ❌ Wrong — bypasses anti-spam guards
gh pr comment PR_NUMBER --repo OWNER/REPO --body "@greptileai review"

# ✅ Correct — single enforcement point with all guards
bash scripts/github/greptile-helper.sh trigger OWNER/REPO PR_NUMBER
```

**Critical**: One trigger path only. Concurrent sessions and API propagation delay caused real Greptile spam incidents, so direct comments are banned.

The helper centralizes the anti-spam guards:
- file lock to prevent concurrent duplicate triggers
- recent-trigger cooldown
- bot-ack detection and grace period
- fail-safe handling for API/rate-limit errors
- re-review logic only when new commits exist after a low-scoring review

Greptile should auto-review new PRs. Use the helper only for re-review after improvements, or when the helper explicitly indicates `needs-re-review`.

## Outcome
Following this pattern results in:
- **Quality validation**: Confirms improvements are effective
- **Spam prevention**: Avoids duplicate raw trigger comments
- **Faster human review**: Pre-validated PRs get approved faster
- **Single enforcement point**: Future guard improvements automatically apply everywhere

Benefits:
- 5-10 minute turnaround for quality check
- Specific feedback on remaining issues
- Score improvement visible (3/5 → 5/5)
- No regression to the old spam-prone raw-comment workflow

## Related
- [gh-pr-review Extension](./gh-pr-review-extension.md) - Manage PR review threads after fixes land
- [gh pr checks --watch Exits 8](./gh-pr-checks-exit-code-8.md) - Pending checks are not failures
- `scripts/github/greptile-helper.sh` - Safe single enforcement point for re-review triggers
