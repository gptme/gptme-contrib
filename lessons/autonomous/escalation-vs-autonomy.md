---
match:
  keywords:
  - "should I ask the user or proceed autonomously"
  - "uncertain whether to escalate or decide"
  - "need human review before proceeding"
  - "risky action requiring approval"
  - "irreversible change decision point"
status: active
---

# Escalation vs Autonomy Decision Framework

## Rule
Proceed autonomously on reversible changes; escalate on irreversible ones or when the cost of being wrong is high.

## Context
When making decisions during autonomous work and unsure whether to proceed or escalate to the human operator.

## Detection
Decision points requiring escalation assessment:
- Deleting files, branches, or tasks
- Changing shared infrastructure (CI, services, configs)
- Making architectural decisions that affect multiple packages
- Responding to external parties (GitHub comments, emails)
- Modifying core configuration or identity files

## Pattern
```text
# Decision matrix:
#
#                    Low cost if wrong    High cost if wrong
# Reversible        → DO IT             → DO IT (but log why)
# Irreversible      → DO IT (carefully) → ESCALATE
#
# Examples:
# - Fix a typo in docs           → DO IT (reversible, low cost)
# - Refactor internal function   → DO IT (reversible, low cost)
# - Delete 50 stale tasks        → ESCALATE (irreversible, moderate cost)
# - Change API contract          → ESCALATE (hard to reverse, high cost)
# - Reply to GitHub issue        → ESCALATE (visible to others)
# - Add new lesson               → DO IT (reversible, low cost)
# - Cancel active task           → ESCALATE (may lose context)
```

## Outcome
Following this pattern leads to:
- **Trust**: The operator can rely on autonomous runs not causing damage
- **Speed**: Most work proceeds without waiting for approval
- **Safety**: Risky actions get human review
- **Learning**: Clear decision points improve over time

## Related
- [Pre-Mortem for Risky Actions](./pre-mortem-for-risky-actions.md) - Quick risk assessment
- [Safe Operation Patterns](./safe-operation-patterns.md) - GREEN/YELLOW/RED classification
