---
match:
  keywords:
  - "while working on X noticed Y could be improved"
  - "discovered tangential improvement opportunity"
  - "also fixed some nearby code while at it"
  - "scope creep during autonomous session"
  - "staying focused on stated objective"
status: active
---

# Scope Discipline in Autonomous Work

## Rule
Complete the stated objective before pursuing tangential improvements, no matter how tempting.

## Context
During autonomous sessions when you discover improvements adjacent to your current task.

## Detection
Observable signals of scope creep:
- "While fixing X, I noticed Y could be improved..."
- Editing files unrelated to the current task
- Session time expanding beyond the original objective
- Multiple unrelated changes in a single commit
- Journal entries covering 5+ unrelated topics

## Pattern
```text
1. Write down current objective before starting
2. When you notice something else:
   - File it as a task or issue (2 min)
   - Do NOT start working on it
3. Return to original objective
4. Only after completing objective: review discovered tasks
5. If time remains: pick ONE from the list

# Exception: If the tangential issue BLOCKS your current work,
# fix only the minimum needed to unblock, then return.
```

## Outcome
Following this pattern leads to:
- **Completed work**: Objectives finish instead of half-done
- **Clean commits**: Each commit has a single purpose
- **Task capture**: Good ideas don't get lost (they become tasks)
- **Predictable sessions**: The operator knows what to expect from each run

## Related
- [Escalation vs Autonomy](./escalation-vs-autonomy.md) - When to proceed vs escalate
- [Session Ending Protocol](../workflow/session-ending-protocol.md) - Clean session handoff
