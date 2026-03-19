---
match:
  keywords:
  - "blocked rate"
  - "PR queue overloaded"
  - "parallel work tracks"
  - "multiple PRs awaiting review"
  - "multiple work tracks blocked"
  - "waiting for reviews or approvals"
status: active
---

# Making Progress Despite Blockers

## Rule
When facing multiple blockers, use parallel tracks, partial progress, and indirect support work rather than declaring complete blockage.

## Context
When primary work items are blocked by external dependencies (approvals, reviews, decisions). Applies in both interactive and autonomous contexts.

## Detection
Observable signals indicating opportunity for alternative progress:
- Multiple items blocked on same person/approval
- Waiting for PR reviews or merge approvals
- Need strategic input or decisions
- Thinking "nothing I can do until X"

## Pattern

**Six strategies**:

1. **Parallel Work Tracks**: Work across 3+ areas; when Track 1 blocked, work on Track 2
2. **Partial Progress**: Can't merge? Add tests, improve docs, address comments preemptively
3. **Indirect Support Work**: Document, prepare runbooks, build monitoring for blocked items
4. **Context Filtering**: Find unblocked work from your task backlog
5. **Continuation Work**: Enhance existing stable systems
6. **Preparation Work**: Write follow-up drafts, research alternatives, gather data

**Automated discovery**: Use a project monitoring service to surface actionable work across repos. See [Project Monitoring Session Patterns](./project-monitoring-session-patterns.md).

## Real Blocker Criteria

Only declare "Real Blocker" after checking ALL six strategies:
- ✓ All parallel tracks exhausted (3+ areas)
- ✓ No partial progress possible
- ✓ No indirect support work available
- ✓ Backlog filtered — no unblocked items found
- ✓ No continuation work available
- ✓ No preparation work possible

## Outcome

Following these patterns results in:
- **Continuous progress**: Always moving forward on something
- **Reduced waiting**: Don't block on single dependency
- **Faster unblocking**: Prepared when blockers lift

## Related
- [Maintaining Readiness During Blocked Periods](../autonomous/maintaining-readiness-during-blocked-periods.md) - Stay useful while waiting on others
- [Strategic Completion Leverage When Blocked](../autonomous/strategic-completion-leverage-when-blocked.md) - Finish the highest-leverage adjacent work
- [Autonomous Session Structure](../autonomous/autonomous-session-structure.md) - Overall session workflow
- [Project Monitoring Session Patterns](./project-monitoring-session-patterns.md) - Automated discovery of actionable work
- [Close the Loop](../patterns/close-the-loop.md) - Automation approach
