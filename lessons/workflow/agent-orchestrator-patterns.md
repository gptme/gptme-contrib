---
status: active
match:
  keywords:
    - "orchestrating multiple agents"
    - "agent team health check"
    - "daily agent standup"
    - "inference utilization across agents"
    - "monitor agent work queue"
    - "coordinate parallel agent sessions"
---

# Agent Orchestrator Patterns

## Rule
As agent orchestrator, your job is to keep the agent team healthy — surfacing blockers and
utilization issues before they compound, not doing reactive status checks.

## Context
In multi-agent setups, one agent typically acts as "chief of staff": reading standups, monitoring
inference spend, coordinating across agents, and batching requests to the human principal.
This lesson documents the recurring patterns for doing that role well.

## Core Responsibilities

### 1. Daily health check (from standups)

Read standups at the start of orchestration sessions:
```bash
# Check who reported in today
ls gptme-superuser/standups/$(date +%Y-%m-%d)/

# Read each file to spot anomalies
cat gptme-superuser/standups/$(date +%Y-%m-%d)/agent-name.md
```

**What to look for**:
- Missing standup = agent didn't run (escalate if 2+ consecutive days)
- `status: degraded` or `status: down` = investigate immediately
- Unusual patterns (e.g., 0 commits over 3+ days for an active agent = something is wrong)
- Imminent high-stakes events mentioned in standups (settlements, deployments, deadlines)

### 2. Generating standups for peripheral agents

Some agents don't have gptme-superuser integration. The orchestrator generates their standups
by SSH-ing into each peripheral agent's host, reading their recent git log and journal entries,
and writing a standup file in the standard format. This can be scripted:
```bash
# Script that SSHes into each peripheral agent, reads their recent commits/journals,
# and writes a formatted standup to gptme-superuser/standups/YYYY-MM-DD/{agent}.md
bash scripts/runs/standup/monitored-agents-standup.sh
```

If you don't have this script, implement the equivalent manually:
1. `ssh agent-host "git -C ~/agent-workspace log --oneline --since='24 hours ago'"` — get recent commits
2. `ssh agent-host "cat ~/agent-workspace/journal/$(date +%Y-%m-%d)*.md"` — read latest journal
3. Synthesize into standup format and write to `gptme-superuser/standups/$(date +%Y-%m-%d)/{agent}.md`

**Peripheral agents**: agents without cross-agent tooling who can't self-report.
**Self-reporters**: agents that write their own standups directly.

For peripheral agents, synthesize their standup from:
- Recent git log (last 24h commits and messages)
- Latest journal entry
- Any session logs accessible via SSH

### 3. Weekly inference utilization review

Run weekly (or when something looks off). Core framework:

| Signal | Healthy | Concern |
|--------|---------|---------|
| Output per session | Commits, features, signals | Multiple sessions with no output |
| Model choice | Appropriate tier for task complexity | Expensive model for routine no-op work |
| Session count | Consistent with workload | 10+ sessions/day for monitoring-only tasks |

**Key question per agent**: Is this inference spend proportionate to the output produced?

When an agent is over-spending (e.g., using Sonnet for routine monitoring with no decisions
to make), send them a message via `gptme-superuser/messages/` with the finding and recommendation.

> **Note on `gptme-superuser`**: This is a *shared git repository* that all agents in the team
> have read/write access to — not a local workspace directory. Messages written there propagate
> to all agents on their next session. See [`inter-agent-communication.md`](./inter-agent-communication.md)
> for the full channel-selection guide (gptme-superuser/messages vs. GitHub issues vs. direct mentions).

```markdown
# Example finding: event-driven agent spending on no-signal sessions
Agent X runs 15 sessions/day. On days with no signals, output is 1 journal commit.
Recommendation: implement external session gating — skip spawning Claude Code when there
are no events, signals, or messages pending. A lightweight shell check (no LLM) decides
whether to spawn at all.
```

### 4. Batching requests to the principal

Don't create noise for the human. Collect multiple asks and send one batched message:

```markdown
# gptme-superuser/messages/YYYY-MM-DD/from-orchestrator-to-principal.md

## High priority (blocking agents or time-sensitive)
- ...

## Watch this week
- ...

## When you have time
- ...
```

**When to escalate immediately** (don't batch):
- Agent service is down
- Critical decision needed (trade, deployment, security)
- Time-sensitive external deadline

**When to batch**:
- API key renewals
- Access grants
- PR reviews
- FYI items that aren't blocking

### 5. Event watches for time-bounded situations

When a significant event is upcoming for an agent (settlement batch, conference, major PR review),
create a dedicated watch task:

```markdown
# tasks/agent-event-watch.md
---
state: active
---
## Event
[What's happening and why it matters]

## How to check
[Which standup file, which field to look for]

## What to do with the result
[Analysis to run, message to send, criteria to evaluate]

## Per-event tracking
| Date | Result | Notes |
...
```

See also: [Agent Event Watch Workflow](./agent-event-watch-workflow.md) for detailed pattern.

## Anti-Patterns

**Don't**: Create daily "status is normal" messages to the principal when nothing has changed.
**Do**: Only message when there's something actionable or notable.

**Don't**: SSH into agent VMs to run commands (beyond read-only log checks).
**Do**: Work through the standup system; agents manage their own operations.

**Don't**: Conflate "agent is quiet" with "agent is broken".
**Do**: Check standup history first; some agents correctly go quiet during blocked periods.

**Don't**: Trigger inference spend for orchestration sessions that find nothing to act on.
**Do**: End the session without committing if the health check finds all agents healthy and nothing actionable.

## Weekly Orchestrator Checklist

```markdown
Monday morning:
- [ ] All standups in for previous week?
- [ ] Any consecutive-missing alerts?
- [ ] Any inference utilization concerns?
- [ ] Any upcoming high-stakes events this week (settlements, conferences, deployments)?
- [ ] Message to principal needed? (batch pending requests)
```

## Outcome

Following this pattern:
- Agent health is visible without each agent needing to check on everyone else
- Inference spend stays proportionate to output produced
- The human principal gets fewer interrupts, but higher-signal ones
- Cross-agent blockers surface before they become multi-day delays

## Related

- [Agent Event Watch Workflow](./agent-event-watch-workflow.md) — structured pattern for monitoring time-bounded events
- [Inter-Agent Communication](./inter-agent-communication.md) — channel selection for agent-to-agent messages
- [Memory Failure Prevention](./memory-failure-prevention.md) — session startup checks
