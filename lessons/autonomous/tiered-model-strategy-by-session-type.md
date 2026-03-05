---
category: autonomous
tags:
- model-selection
- inference-cost
- session-routing
- efficiency
match:
  keywords:
  - model tier
  - haiku vs sonnet
  - inference cost
  - session type
  - routine monitoring
  - model selection
  - cost optimization
---

# Tiered Model Strategy by Session Type

## Rule
Not all autonomous sessions require the same reasoning capability. Route sessions to lighter models (Haiku) for routine monitoring work, and reserve Sonnet/Opus for sessions that genuinely require complex reasoning.

## Context
Autonomous agents often run on a fixed schedule regardless of whether there's meaningful work to do. Routine monitoring sessions (check prices, check inbox, no signals) don't need frontier reasoning — but agents default to whatever model they're configured with, often Sonnet.

This creates a systematic waste pattern: expensive model used for templated, no-decision sessions.

## Detection
Observable signals that tiered routing would help:
- Agent runs many sessions per day with near-identical outputs
- Most sessions produce 0 decisions, 0 signals, 0 meaningful outputs
- Work follows a fixed tool sequence regardless of results
- Session cost per meaningful output is high relative to session cadence

## Pattern

### Session Type Classification

Classify sessions at startup before doing substantial work:

**Type A: Routine monitoring** (Haiku appropriate)
- No signals above threshold in last 24h
- No time-sensitive events (settlement, deadlines) within 12h
- No messages requiring reasoning
- Standard scan → nothing to do → write journal → exit

**Type B: Decision-required** (Sonnet appropriate)
- Signal above threshold detected
- Time-sensitive event imminent (settlement, deadline)
- Message from collaborator requiring analysis
- Complex analysis or strategy adjustment needed

### Implementation Pattern

```python
# At session startup, run lightweight signal check first
signals = check_signals(min_edge=0.05)
messages = check_inbox()
imminent_event = check_upcoming_events(within_hours=12)

if signals or messages or imminent_event:
    session_type = "decision-required"  # Use Sonnet
else:
    session_type = "routine-monitoring"  # Use Haiku or skip
```

### Session Cadence Optimization

Dead periods (zero signals, no imminent events) don't need sub-hour monitoring cadence. Binary options settling in 24h don't require 30-minute checks. Consider:
- **Active period** (signal detected, event within 12h): Every 15-30 min
- **Normal period** (watching, no signals): Every 1-2 hours
- **Dead period** (extended no signals): Every 2-4 hours

### Cost Impact Example

An agent running 16 sessions/day on Sonnet at ~$0.50-1.50/session = **$8-24/day**.

If 75% of sessions are routine monitoring (Type A) that can use Haiku (~10× cheaper):
- 4 Sonnet sessions/day × $1.00 avg = $4.00
- 12 Haiku sessions/day × $0.10 avg = $1.20
- Total: ~$5.20/day vs $8-24/day → **50-75% savings**

With no loss of quality on decisions that matter.

## Anti-Pattern

**Wrong: Model-agnostic flat cadence**
```text
Every 30 min, always:
  - Check signals (0 found)
  - Check inbox (empty)
  - Write journal (nothing new)
  - Commit ("S220 — no signals")
  [Repeat 16x/day, every day, on Sonnet]
```

**Correct: Session type routing**
```text
Startup check:
  - Signals? No. Messages? No. Imminent settlement? No.
  → Route to Haiku (or skip session entirely)
  → Write minimal journal, exit early

Startup check (different day):
  - Signals? YES — BTC edge 7.2%, near strike
  → Route to Sonnet
  → Full analysis, trade decision
```

## Outcome
Following this pattern results in:
- **Significant cost reduction**: 50-75% on inference spend for monitoring-heavy agents
- **Quality preserved**: Sonnet/Opus available when decisions matter
- **Cleaner signal**: Sessions with complex reasoning are identifiable vs. routine checks
- **Appropriate cadence**: Check less often when there's nothing to check

## Related
- [Blocked Period Status Check Trap](./blocked-period-status-check-trap.md) - Avoiding no-op commits
- [Strategic Focus During Autonomous Sessions](./strategic-focus-during-autonomous-sessions.md) - Prioritizing work

## Origin
2026-03-05: Extracted from analysis of Gordon's trading agent inference patterns. Gordon ran 16 sessions/day on Sonnet with ~12-14 producing zero signals. Tiered model recommendation was sent as a formal optimization suggestion.
