---
status: active
match:
  keywords: ["event watch", "watch task", "settlement watch", "trigger date", "standup result", "high-stakes event", "analyze results"]
---

# Agent Event Watch Workflow

## Rule

When a significant upcoming event for an agent could change strategy (trade settlement, PR merge,
deployment result, experiment outcome), create a named watch task that documents exactly what to
look for and what action to take when the result is in.

## Context

Applies to any high-stakes event where:
- The result will inform a decision (scale / hold / reduce / proceed / revert)
- The data will appear in a standup file, log, or external source at a known future date
- Action is needed promptly once the result is in

Examples: trade settlement batches, first real-capital deploys, model experiment conclusions,
infrastructure rollouts, critical PR merges.

## Workflow

### Step 1: Create the watch task

When the event is identified (usually from standup monitoring or planning):

```bash
# Create task file
touch tasks/AGENT-event-name.md
```

Structure the task with:
1. **Context** — what the event is, why it matters, current baseline metrics
2. **What to watch for** — specific checklist items in the upcoming standup/log
3. **Analysis to perform** — calculations or comparisons to run once data is in
4. **Action upon trigger** — exactly what to do (read file → analyze → message who)

```markdown
## Action (upon trigger date)
1. Read results from `{coord-repo}/standups/YYYY-MM-DD/agent.md`
2. Perform the analysis (win rate, PnL delta, comparison to baseline)
3. Message {stakeholder} with findings + recommendation
4. Update this task to done
```

### Step 2: Capture baseline context before the event

While waiting, document the metrics the analysis will need:
- Current benchmark values (paper win rate, expected performance)
- Current state near critical thresholds (prices vs. strikes, metrics vs. targets)
- Expected outcome range (what does success look like?)

Store this in the task body so the session handling the trigger has full context
without needing to re-research.

### Step 3: At the start of each session, check if triggered

In Phase 1 of the session, scan for active watch tasks:
```bash
gptodo status --compact | grep "watch"
```

Then check if the trigger condition is met:
```bash
# Example: check for standup file from trigger date
ls {coord-repo}/standups/YYYY-MM-DD/agent.md 2>/dev/null || echo "Not yet"
```

### Step 4: When triggered — act immediately

Once the trigger fires, don't defer. Act within the same session.

**For self-reporting agents (Alice, Bob — write their own standups)**:
1. Read the standup
2. Do the analysis
3. Write findings
4. Message the relevant party
5. Mark task done

**For peripheral/monitored agents (those that don't self-report)**:
1. Generate their standup first via the monitoring script
   - If the event just happened, their post-event sessions may not have run yet — wait or retry
2. Verify the standup includes post-event data (not just pre-event sessions)
3. Then proceed: analysis → findings → message → mark done

The value of the watch task is ensuring the analysis happens promptly with full context,
not a week later when data has staled and context is lost.

## Analysis Framework for Decision-Driving Events

| Metric | What to compute | Why it matters |
|--------|----------------|----------------|
| Primary outcome | Did the event succeed/fail? | Binary signal for decision |
| Comparison to baseline | How did it track the benchmark? | Validates model/strategy |
| Edge case behavior | Were the high-confidence calls right? | Calibration check |
| Resource outcome | Cost/capital deployed vs. result | Sizing appropriateness |

**Scale-up signal**: Outcome tracks benchmark (within expected variance), calibration holds.
**Hold signal**: Outcome below benchmark but within noise, small sample.
**Reduce/revert signal**: Systematic underperformance, miscalibration, or technical failures.

## Anti-Patterns

**Don't**: Create a watch task but check it "when I get to it"
**Do**: Treat the trigger as highest-priority work in the session it appears

**Don't**: Skip the baseline context capture, then re-research at trigger time
**Do**: Document baseline metrics in the task when creating it

**Don't**: Create watch tasks for routine events (daily monitoring, normal output)
**Do**: Only create watch tasks for events that materially change a decision

**Don't**: Check for the trigger file in a separate "follow-up" session if it appears mid-week
**Do**: Build the watch check into Phase 1 of every session during the watch period

## Outcome

Following this pattern results in:
- **Timely analysis**: Results are processed promptly in the same session the trigger fires, not days later
- **Full context**: Baseline metrics are pre-captured in the task, so no re-research is needed at trigger time
- **Clear decisions**: Structured framework maps outcome to action (scale / hold / reduce / revert)
- **Reliable follow-through**: Watching is built into Phase 1 of every session — triggers aren't missed

## Related

- [Inter-Agent Communication](./inter-agent-communication.md) — messaging findings to relevant parties
- [Autonomous Session Structure](../autonomous/autonomous-session-structure.md) — Phase 1 trigger checks
