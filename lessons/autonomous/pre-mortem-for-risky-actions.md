---
match:
  keywords:
  - "about to delete or remove files"
  - "force push or destructive git operation"
  - "modifying production infrastructure"
  - "risk assessment before action"
  - "what could go wrong with this change"
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
- Bulk operations (deleting multiple files, tasks, branches)
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

## Outcome
Following this pattern leads to:
- **Prevented disasters**: Catches mistakes before they happen
- **Confidence**: Execute risky actions with full awareness
- **Recovery readiness**: Know the rollback plan before you need it
- **Trust**: History of zero-incident autonomous operations

## Related
- [Escalation vs Autonomy](./escalation-vs-autonomy.md) - When to ask for help
- [Safe Operation Patterns](./safe-operation-patterns.md) - GREEN/YELLOW/RED classification
