---
status: active
match:
  keywords:
    - event watch
    - watch task
    - settlement watch
    - upcoming event
    - event trigger
---

# Agent Event Watch Workflow

## Rule
When a significant upcoming event could change strategy or trigger action, create a named watch task documenting what to look for and what to do when the result appears.

## Context
Events like trade settlements, deployment completions, PR reviews, or external data arrivals that are time-bounded, appear in a predictable location, and map to a decision or action.

## Detection
- An upcoming event produces data that triggers a decision (scale/hold/reduce)
- The result appears at a known location at a predictable time
- Missing the analysis means acting on stale information
- NOT for routine daily monitoring already handled by scheduled sessions

## Pattern

**Watch task structure** (4 sections):
1. **Context**: What the event is, why it matters, baseline metrics
2. **Trigger**: Exact condition that fires the task (file path, PR state, etc.)
3. **Analysis**: What to compute once data is in
4. **Action**: Exactly what to do, in order

**Trigger check** — built into Phase 1 of every session:
```bash
# File-based trigger
[[ -f "path/to/result" ]] && echo "TRIGGERED"

# GitHub trigger
gh pr view 123 --json state -q '.state'
```

**When triggered**: Act in the same session. Don't defer. Pre-defined thresholds map outcome to action (scale up / hold / reduce).

## Outcome
- Timely analysis — results processed the session they appear
- Full context — baselines pre-captured, no re-research needed
- Clear decisions — pre-defined thresholds prevent post-hoc bias

## Related
- Companion doc: [knowledge/lessons/autonomous/agent-event-watch-workflow.md](../../knowledge/lessons/autonomous/agent-event-watch-workflow.md)
- [Autonomous Session Structure](./autonomous-session-structure.md)
