---
match:
  keywords:
    - "structured monitoring triage workflow"
    - "ACT vs DEF notification classification"
    - "monitoring session derailing into investigation"
    - "multi-project notification triage"
status: active
---

# Project Monitoring Session Patterns

## Rule
Use systematic classification and time-boxed execution for monitoring sessions: classify notifications as ACT (action required) vs DEF (deferred), then execute only ACT items.

## Context
When managing multiple active projects with notifications, PRs, and issues requiring periodic triage — not for primary creative/strategic work.

## Detection
- Multiple GitHub notifications from different repos needing triage
- Risk of spending entire session on maintenance vs primary work
- No system for distinguishing urgent from informational items

## Pattern

### 4-Phase Monitoring (12-15 min total)

1. **Investigation** (3 min): Quick context scan across all items
2. **Classification** (2 min): Binary ACT/DEF decision per item
   - **ACT**: CI failures blocking your PRs, critical bugs, communication loops to close
   - **DEF**: PRs under review, issues assigned to others, merged work
3. **Execution** (7 min): ACT items only. Defer complex work to dedicated sessions.
4. **Communication** (2 min): Close loops, update stakeholders, brief journal entry

### Anti-pattern
Spending 20+ minutes investigating each notification before classifying. Quick context is sufficient for classification — deep investigation only during execution of ACT items.

## Outcome
- Critical blockers resolved quickly in 12-15 min sessions
- Monitoring doesn't derail creative/strategic sessions
- Important items not missed across multiple projects

## Related
- [Autonomous Session Structure](../autonomous/autonomous-session-structure.md) — General session management
