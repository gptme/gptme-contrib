---
description: "Before taking irreversible or high-blast-radius actions, name what could go wrong and verify safeguards are in place"
match:
  keywords:
  - pre-mortem for risky actions
  - "force push or destructive git operation"
  - "modifying production infrastructure"
  - "risk assessment before action"
  - "what could go wrong with this change"
  - "irreversible change"
  - "force push"
  - "rm -rf"
  - "DROP TABLE"
  - "production change"
  session_categories: [code, infrastructure, cleanup]
target_grade: alignment
status: active
---

# Pre-Mortem for Risky Actions

## Rule
Before executing any action that could cause data loss or service disruption, spend 30 seconds imagining it went wrong and identify what you'd wish you had checked.

## Context
Before executing destructive operations, infrastructure changes, or modifications to shared state.

## Detection
Actions that warrant a pre-mortem:
- `git push --force`, `git reset --hard`, `rm -rf`
- Modifying systemd services or cron jobs
- Changing environment variables or secrets
- Database migrations or schema changes
- Modifying pre-commit hooks or CI config

## Pattern
```text
# Before risky action, ask:
1. What happens if this goes wrong?
2. Can I undo it? How?
3. Is there a dry-run option?
4. Have I backed up what I'm about to change?
5. Am I operating on the right target? (check twice)

# Example: Before deleting 50 stale branches
# 1. Wrong: could lose unmerged work → check for unmerged commits
# 2. Undo: git reflog for ~90 days → acceptable
# 3. Dry-run: list branches first → do this
# 4. Backup: N/A (reflog is backup) → ok
# 5. Target: only task/job-* pattern → verify pattern
```

## Procedure
The Pattern above is a mental checklist — these steps are how you execute it:

1. **Name the target and blast radius** — state exactly what will change and what the failure would break.
2. **Choose a verification surface before acting** — identify a dry-run, target listing, or equivalent read-only command that shows whether you are aimed at the right thing.
3. **Choose a rollback path before acting** — name the concrete undo path (`git reflog`, backup restore, service restart, config revert) before the risky command runs.
4. **Run the verification surface and inspect it** — if the output does not match intent, abort: document the mismatch in your journal and surface it as a task blocker rather than proceeding.
5. **Execute only after the checks pass** — perform the risky action, then immediately verify post-action state and use the rollback path if the result is wrong.

## Outcome
Following this pattern leads to:
- **Prevented disasters**: Catches mistakes before they happen
- **Confidence**: Execute risky actions with full awareness
- **Recovery readiness**: Know the rollback plan before you need it
- **Trust**: History of zero-incident autonomous operations

## Related
- [Escalation vs Autonomy](./escalation-vs-autonomy.md) - When to ask for help
- [Safe Operation Patterns](./safe-operation-patterns.md) - GREEN/YELLOW/RED classification
