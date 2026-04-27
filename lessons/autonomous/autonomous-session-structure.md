---
match:
  keywords:
  - autonomous session structure
  - 4-phase session
  - session phase planning
  - structured autonomous session
  - autonomous work phases
  session_categories: [autonomous, self-review]
status: active
---

# Autonomous Session Structure

## Rule
Use a structured 4-phase approach for autonomous sessions: status check, task selection, execution, commit & complete.

## Context
When operating autonomously with limited time (25-35 minutes) to complete meaningful work.

## Detection
- Session starting without checking git status or recent context
- Jumping to work without task selection
- Session ending with uncommitted/unpushed work
- Treating "waiting for response" as "blocked" (they're different)
- Claiming "all blocked" when other work exists

## Pattern

| Phase | Time | Actions |
|-------|------|---------|
| **1. Status** | ~3 min | `git status`, commit loose ends, check recent journal |
| **2. Select** | ~4 min | CASCADE: queue → notifications → `gptodo ready` |
| **3. Execute** | ~22 min | Focused work, journal entries, task updates |
| **4. Complete** | ~4 min | Stage specific files, commit, push to origin |

**CASCADE selection**: Check PRIMARY (work queue), SECONDARY (notifications), TERTIARY (`gptodo ready`). First unblocked work wins.

**Waiting ≠ blocked**: If waiting on a response, set `waiting_for` in task metadata and move to the next available task.

```bash
# Phase 4 — always explicit files, never git add .
git add specific-files
git commit -m "type(scope): description"
git push origin master
```

## Outcome
- Every session creates documented, committed value
- No lost work from uncommitted changes
- Clear handoffs between sessions
- Structured approach maximizes productive time

## Related
- Companion doc: [knowledge/lessons/autonomous/autonomous-session-structure.md](../../knowledge/lessons/autonomous/autonomous-session-structure.md)
- [Autonomous Session Pivot Strategies](./autonomous-session-pivot-strategies.md)
- [Git Workflow](../workflow/git-workflow.md)
