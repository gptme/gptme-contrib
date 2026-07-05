# Agent Event Watch Workflow — Companion Doc

Full implementation details for the primary lesson at `lessons/autonomous/agent-event-watch-workflow.md`.

## Watch Task File Structure

```markdown
---
state: active
created: 2025-03-06T10:00:00+00:00
tags: [event-watch]
---
# Watch: [Event Name]

## Context
[Agent] has [current state]. [Event] expected at [time].
Paper baseline: [metrics]. Current: [metrics].

## Trigger
Result appears at: [file path or URL]
Contains: [what data to look for]

## Analysis
- Compare [metric] vs. baseline
- Check if [threshold criteria] met
- Compute [relevant delta]

## Action (upon trigger)
1. Read the result file/source
2. Run analysis (compare vs. baseline)
3. Message stakeholder with findings + recommendation
4. Mark task done
```

## Step-by-Step Workflow

### Step 1: Create the watch task
When the event is identified, create a task file:
```bash
touch tasks/AGENT-event-name-watch.md
```

### Step 2: Pre-capture baseline context
While waiting, document metrics the analysis will need:
- Current benchmarks and reference values
- Expected outcome range (what success looks like)
- Context that would be stale or hard to recover later

This ensures the analysis session is self-contained.

### Step 3: Check for trigger at session start
During Phase 1 status check:
```bash
gptodo status --compact | grep "watch"
```

Then check the trigger condition:
```bash
# Standup-based
[[ -f "standups/$(date +%Y-%m-%d)/agent.md" ]] && echo "TRIGGERED"

# File-based
[[ -f "path/to/result" ]] && echo "TRIGGERED"

# GitHub-based
gh pr view 123 --json state -q '.state'
```

### Step 4: When triggered — act immediately
Don't defer to a follow-up session. Process in the same session:

1. Read the result file/source
2. Compute analysis (compare vs. pre-captured baseline)
3. Write findings (journal entry or task update)
4. Message relevant party with recommendation
5. Mark watch task done
6. If more batches expected, create the next watch task

**Why same-session**: Context is fresh, metrics haven't shifted, stakeholder decisions can happen while the situation is live.

### For agent orchestrators
If the result agent doesn't self-report:
```bash
# Generate standup from remote agent
bash scripts/runs/standup/monitored-agents-standup.sh --date YYYY-MM-DD

# Verify post-event data exists
grep -i "settlement\|result" standups/YYYY-MM-DD/agent.md
```

If the event just happened but the agent hasn't run post-event, wait for the next session cycle.

## Trigger Types

| Event | Where result appears | Trigger check |
|-------|---------------------|---------------|
| Settlement/batch | Agent standup file | `ls standups/DATE/agent.md` |
| PR/code review | GitHub PR status | `gh pr view N --json state` |
| Deployment | Health endpoint | `curl -s .../health` |
| External data | Known file path | `[[ -f path/to/data ]]` |
| Scheduled job | Log file | `grep -q "SUCCESS" logfile` |

## Decision Framework

Map outcome to action using pre-defined thresholds:

| Outcome | Signal | Action |
|---------|--------|--------|
| Better than expected | Metrics exceed threshold | Scale up / proceed |
| Acceptable range | Metrics meet minimum | Hold / monitor |
| Below threshold | Metrics miss minimum | Reduce / escalate |

Pre-define thresholds when creating the watch task — don't decide criteria after seeing the result.

## Anti-Patterns

- Create watch task but check "when I get to it" → treat trigger as highest-priority
- Skip baseline capture, re-research at trigger time → pre-document baselines
- Create watches for routine events → only for events that materially change decisions
- Set outcome thresholds after seeing the result → pre-define at creation time

## Origin
2026-03-06: Extracted from monitoring a trading agent's first real-capital settlement batch.
Generalized from financial context to cover any orchestrator-agent event watch scenario.
