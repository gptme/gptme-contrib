---
match:
  keywords:
  - "classify operation before executing"
  - "GREEN YELLOW RED classification"
  - "safe to execute autonomously"
  - "requires human approval"
  - classification
  - GREEN
  - YELLOW
  - RED
status: active
---

# Safe Operation Patterns

## Rule
Classify operations as GREEN (always safe), YELLOW (safe with patterns), or RED (requires human) before executing.

## Context
During autonomous operation when selecting and executing any task or action.

## Detection
- About to execute any operation autonomously
- Task involves external interaction
- Planning actions with potential impact
- Tool returns empty data (connection OK but no results)

## Pattern

Classify before executing, then follow appropriate action:

### Classification Table

| Classification | Criteria | Action |
|----------------|----------|--------|
| **GREEN** ✓ | Safe, reversible, autonomous | Execute immediately |
| **YELLOW** ⚠ | Safe with established pattern | Follow specific pattern (e.g., twitter-best-practices.md) |
| **RED** ❌ | Financial, irreversible, human judgment | Escalate, don't execute |

**Examples**:
- Code change → GREEN (reversible)
- Tweet update → YELLOW (has pattern)
- Execute trade → RED (financial)

### Empty Data Debugging

When tool connects but returns zero results:

| Check | Finding | Action |
|-------|---------|--------|
| `curl <source_url>` | Source has data | Tool parsing bug |
| `curl <source_url>` | Source empty | Genuinely empty, move on |
| `curl <source_url>` | Different format | Update tool config |

Skip raw inspection if: connection failed, tool reports errors, multiple tools confirm emptiness.

## Outcome
- **Safe autonomy**: No dangerous operations without human
- **Efficient progress**: GREEN tasks proceed immediately
- **Efficient debugging**: Distinguish source vs tool bugs quickly

## Related
- [Autonomous Operation Safety](./autonomous-operation-safety.md) - The "lethal trifecta" pattern
- [Escalation vs Autonomy](./escalation-vs-autonomy.md) - Decision framework
