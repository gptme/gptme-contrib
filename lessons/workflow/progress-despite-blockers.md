---
match:
  keywords:
  - "blocked rate"
  - "awaiting review"
  - "PR queue overloaded"
  - "all tasks waiting"
  - "find unblocked work"
  - "alternative progress"
status: active
---

# Making Progress Despite Blockers

## Rule
When facing multiple blockers, use parallel tracks, partial progress, and indirect support work rather than declaring complete blockage.

## Context
During autonomous operation when primary work items are blocked by external dependencies (approvals, reviews, decisions).

## Detection
Observable signals indicating opportunity for alternative progress:
- Multiple items blocked on same person/approval
- Waiting for PR reviews or merge approvals
- Need strategic input or decisions
- Thinking "nothing I can do until X"

## Pattern

**Six strategies** (detailed examples in companion doc):

1. **Parallel Work Tracks**: Work across 3+ areas; when Track 1 blocked, work on Track 2
2. **Partial Progress**: Can't merge? Add tests, improve docs, address comments preemptively
3. **Indirect Support Work**: Document, prepare runbooks, build monitoring for blocked items
4. **Context Filtering**: Find unblocked work from your task backlog
5. **Continuation Work**: Enhance existing stable systems
6. **Preparation Work**: Write follow-up drafts, research alternatives, gather data

**Automated discovery**: Set up a project monitoring service that finds actionable work across repos.

## Real Blocker Criteria

Only declare "Real Blocker" after checking ALL six strategies:
- ✓ All parallel tracks exhausted (3+ areas)
- ✓ No partial progress possible
- ✓ No support/preparation work available
- ✓ No continuation opportunities

## Outcome

Following these patterns results in:
- **Continuous progress**: Always moving forward on something
- **Reduced waiting**: Don't block on single dependency
- **Faster unblocking**: Prepared when blockers lift

## Related
- [Autonomous Session Structure](../autonomous/autonomous-session-structure.md) - Overall session workflow
- [Close the Loop](../patterns/close-the-loop.md) - Automation approach
