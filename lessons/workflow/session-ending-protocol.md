---
match:
  keywords: ["session ending", "land the plane", "session complete", "finish session", "ending session"]
status: active
---

# Session Ending Protocol

## Rule
Before completing any session, follow the "Land the Plane" checklist to ensure clean handoff.

## Context
At the end of any autonomous run, interactive session, or substantial work block.

## Detection
Observable signals that you're ending a session:
- About to use `complete` tool
- Wrapping up substantial work
- Time/context budget running low
- Natural stopping point reached

## Pattern
Execute checklist before completing:

```text
Session Ending Checklist ("Land the Plane"):

1. FILE REMAINING WORK
   - [ ] Create tasks/issues for discovered follow-up work
   - [ ] Document any blockers encountered
   - [ ] Note deferred decisions or trade-offs

2. QUALITY VERIFICATION
   - [ ] Pre-commit hooks pass (or document failures)
   - [ ] Tests pass for changed code
   - [ ] No obvious broken state

3. UPDATE TRACKING
   - [ ] Task tracker updated with status
   - [ ] Progress documented (journal, notes, etc.)
   - [ ] Issues/PRs updated with progress

4. GIT STATE CLEAN
   - [ ] All intended changes committed
   - [ ] No untracked files that should be tracked
   - [ ] Branch state is coherent

5. RECOMMEND NEXT WORK
   - [ ] Identify logical next task
   - [ ] Provide context for next session
```

## Outcome
Following this protocol ensures:
- **Clean handoff**: Next session starts from known state
- **No lost work**: Follow-up items captured before context lost
- **Quality maintained**: Verification prevents broken commits
- **Continuity**: Clear guidance for next session

## Related
- [Pre-Landing Self-Review](./pre-landing-self-review.md) - Review for substantial changes
