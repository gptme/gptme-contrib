---
match:
  keywords:
    - event watch
    - watch task
    - settlement watch
    - upcoming event
    - scheduled result
    - standup result
    - event trigger
    - monitor for result
    - waiting for outcome
status: active
---

# Agent Event Watch Workflow

## Rule

When a significant upcoming event could change strategy or trigger action, create a named watch
task documenting exactly what to look for and what to do when the result appears.

## Context

When orchestrating agents or monitoring external processes, some events are time-bounded and
high-stakes: a trade batch settling, a deployment going live, a PR review completing, a
conference result arriving. These events:
- Appear in a predictable location (standup file, GitHub, log) at a known future time
- Require prompt analysis once the result is in
- Map to a decision or action (scale up, rollback, escalate)

Without a structured watch task, results get processed late or with insufficient context —
the analysis happens days after the event when memory is stale and context must be re-researched.

## Workflow

### Step 1: Create the watch task when the event is identified

```bash
# Create task file with descriptive name
touch tasks/AGENT-event-name-watch.md
```

Structure the task with four sections:

**1. Context** — what the event is, why it matters, current baseline metrics
```markdown
## Context
Gordon has 24 open positions, -$59.44 MTM. First deep-ITM batch settles March 7.
Paper baseline: 70.8% win rate (418 trades). Real cumulative: +$56.80 USDC.
```

**2. Trigger definition** — exactly what condition fires the task
```markdown
## Trigger
Standup appears at: gptme-superuser/standups/2026-03-07/gordon.md
Contains: settlement data for March 7 batch
```

**3. Analysis to perform** — what to compute once data is in
```markdown
## Analysis
- Win rate vs. paper baseline
- PnL delta vs. expected
- Scale trigger criteria met?
```

**4. Action upon trigger** — exactly what to do, in order
```markdown
## Action (upon trigger)
1. Read the standup/result file
2. Run analysis (compare vs. baseline)
3. Message stakeholder with findings + recommendation
4. Mark task done
```

### Step 2: Pre-capture baseline context before the event

While waiting, document the metrics the analysis will need:
- Current benchmarks, baselines, or reference values
- Expected outcome range (what does success look like?)
- Any context that will be stale or hard to recover later

This ensures the analysis session is self-contained — no re-research when the trigger fires.

### Step 3: Check for trigger at the start of each session

During Phase 1 status check, scan for active watch tasks:

```bash
./scripts/tasks.py status --compact | grep "watch"
```

Then check if the trigger condition is met:

```bash
# For standup-based triggers:
ls gptme-superuser/standups/$(date +%Y-%m-%d)/gordon.md 2>/dev/null || echo "Not yet"

# For file-based triggers:
[[ -f "path/to/result/file" ]] && echo "TRIGGERED" || echo "Not yet"

# For GitHub-based triggers:
gh pr view 123 --json state -q '.state'  # Check if PR merged
```

### Step 4: When triggered — act in the same session

Once the trigger fires, don't defer it to a follow-up session. Process immediately.

**Standard action sequence:**
1. Read the result file/source
2. Compute the analysis (compare vs. pre-captured baseline)
3. Write findings (journal entry or task update)
4. Message the relevant party with recommendation
5. Mark the watch task done
6. If more batches expected, create the next watch task

**Why same-session matters**: Analysis is highest-quality when the trigger fires.
Context is fresh, prices/metrics haven't shifted, and the stakeholder's decision
can happen while the situation is still live.

**For agent orchestrators monitoring peripheral agents:**

If the result comes from an agent that doesn't self-report (doesn't write to shared files),
you may need to generate the standup first:

```bash
# Generate standup from agent's remote system
bash scripts/runs/standup/monitored-agents-standup.sh --date YYYY-MM-DD

# Verify it includes post-event data (not just pre-event sessions)
grep -i "settlement\|result\|event" gptme-superuser/standups/YYYY-MM-DD/agent.md
```

If the event just happened, the agent may not have run a post-event session yet.
In that case, wait until the next scheduled session cycle before triggering analysis.

## Watch Task Trigger Types

| Event type | Where result appears | Trigger check |
|-----------|----------------------|---------------|
| Periodic settlement/batch | Agent standup file | `ls standups/DATE/agent.md` |
| PR/code review | GitHub PR status | `gh pr view N --json state` |
| Deployment | Service health endpoint | `curl -s .../health` |
| External data arrival | Known file path | `[[ -f path/to/data ]]` |
| Scheduled job | Log file | `grep -q "SUCCESS" logfile` |

## Decision Framework

Once the result is in, map outcome to action:

**Three-outcome framework** (generalizes to most watch events):

| Outcome quality | Signal | Action |
|----------------|--------|--------|
| Better than expected | Metrics exceed threshold | Scale up / proceed / approve |
| Within acceptable range | Metrics meet minimum criteria | Hold / continue / monitor |
| Below threshold | Metrics miss minimum criteria | Reduce / rollback / escalate |

Pre-define these thresholds in the watch task when creating it — don't decide criteria
after seeing the result (post-hoc threshold setting biases decisions).

## Anti-Patterns

**Don't**: Create a watch task but check "when I get to it"
**Do**: Treat trigger as highest-priority work in the session it fires

**Don't**: Skip baseline context capture, then re-research at trigger time
**Do**: Pre-document baselines when creating the watch task

**Don't**: Create watch tasks for routine events (daily monitoring, normal output)
**Do**: Only create watch tasks for events that materially change a decision

**Don't**: Check for the trigger file in a separate follow-up session
**Do**: Build the trigger check into Phase 1 of every session during the watch period

**Don't**: Set outcome thresholds after seeing the result
**Do**: Pre-define scale/hold/reduce criteria in the task file at creation time

## Outcome

Following this pattern results in:
- **Timely analysis**: Results processed promptly in the same session the trigger fires
- **Full context**: Baseline metrics pre-captured, no re-research needed at trigger time
- **Clear decisions**: Pre-defined thresholds map outcome to action
- **Reliable follow-through**: Watch check built into Phase 1 of every session

## Related
- [Autonomous Session Structure](./autonomous-session-structure.md) — Phase 1 status check
- [Strategic Focus During Autonomous Sessions](./strategic-focus-during-autonomous-sessions.md) — Prioritization
- [Rule-Driven Session Gating](./rule-driven-session-gating.md) — Lightweight trigger checks

## Origin
2026-03-06: Extracted from monitoring a trading agent's first real-capital settlement batch.
The pattern emerged from needing to track an event that would materially change a scaling
decision, with results appearing in a standup file at a known future date. Generalized from
the financial context to cover any orchestrator-agent event watch scenario.
