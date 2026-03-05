---
status: active
category: autonomous
tags:
- inference-cost
- session-management
- monitoring
- efficiency
- orchestration
match:
  keywords:
  - inference cost
  - monitoring sessions
  - session gating
  - no-op sessions
  - skip session
  - session orchestration
  - routine monitoring
  - cost optimization
---

# Rule-Driven Session Gating for Monitoring Agents

## Rule
Don't spawn agent sessions for routine monitoring unless external conditions warrant it. Use lightweight rule-based checks outside gptme to decide whether a session is needed at all — and skip entirely when there's nothing to decide.

## Context
Monitoring agents (price watchers, inbox monitors, status trackers) often run on a fixed schedule regardless of whether there's meaningful work to do. Each session consumes inference budget even when it produces zero decisions, zero commits, zero value.

The fix is **not** to use a lighter model — lighter models (e.g., Haiku) are unreliable at tool use and can't be trusted for agent tasks. The fix is to skip sessions when there's nothing to decide, using lightweight external checks *before* spawning gptme.

This is already how gptme's autonomous and project monitoring runs work: timers, conditions, and rule-based triggers determine when to run.

## Detection
Observable signals that session gating would reduce waste:
- Agent runs many sessions per day with near-identical, low-value outputs
- Most sessions produce 0 decisions, 0 signals, 0 commits
- Session logs are templated: "checked X, nothing to report"
- Work follows a fixed tool sequence regardless of findings

## Pattern

### Architecture: External Orchestrator + Conditional Spawn

The orchestrator runs a **lightweight signal check** (no LLM, just API calls or file reads), then decides whether to spawn gptme:

```bash
#!/bin/bash
# monitor-and-gate.sh — run this on a schedule instead of gptme directly

# Lightweight checks (fast, no inference cost)
SIGNALS=$(check-signals --min-edge 0.05 --count)
MESSAGES=$(check-inbox --unread --count)
EVENTS=$(check-upcoming-events --within-hours 12 --count)

if [[ "$SIGNALS" -gt 0 || "$MESSAGES" -gt 0 || "$EVENTS" -gt 0 ]]; then
    # Something to decide — spawn full session
    gptme --model sonnet "Agent run: signals=$SIGNALS messages=$MESSAGES events=$EVENTS"
else
    # Nothing to do — skip session entirely
    echo "$(date -u): No signals, skipping session" >> session-gate.log
fi
```

### Implementing Signal Checks

The `check-*` commands above are abstractions — implement them as simple scripts with no LLM cost:

**File-based signal (standup or event file appeared):**
```bash
check_standup_ready() {
    local agent="$1" date="$2"
    [[ -f "gptme-superuser/standups/${date}/${agent}.md" ]] && echo 1 || echo 0
}
AGENT_NAME="your-agent"  # Replace with your agent's identifier
EVENTS=$(check_standup_ready "$AGENT_NAME" "$(date +%Y-%m-%d)")
```

**Price proximity check (external API + arithmetic):**
```bash
check_price_near_strike() {
    # Returns 1 if current price is within threshold% of strike
    local current="$1" strike="$2" threshold="${3:-0.05}"
    python3 -c "
c=$current; s=$strike; t=$threshold
print(1 if abs(c - s) / s <= t else 0)"
}
BTC_PRICE=$(curl -sf 'https://api.example.com/price/btc' | jq -r '.usd')
SIGNALS=$(check_price_near_strike "$BTC_PRICE" 72000 0.01)
```

**New message check (file modification time):**
```bash
check_new_messages() {
    find gptme-superuser/messages/ -newer .last-check -name "to-*.md" 2>/dev/null | wc -l
}
MESSAGES=$(check_new_messages)
touch .last-check  # update watermark after checking
```

These run in milliseconds with zero inference cost.

### Session Cadence Optimization

Pair session gating with adaptive cadence:
- **Active period** (signal detected, event within 12h): Check every 15-30 min
- **Normal period** (watching, no signals): Check every 1-2 hours
- **Dead period** (extended no signals): Check every 2-4 hours

Even the "check" is cheap (shell script, single API call) — only the gptme spawn has significant cost.

### Cost Impact Example

An agent running 16 sessions/day on Sonnet at ~$0.75/session = **$12/day**.

If 75% of sessions are skippable (no signals, no events):
- 4 sessions/day × $0.75 avg = **$3.00/day**
- 12 sessions skipped = $0 cost
- Total: **$3/day vs $12/day → 75% savings**

With *no loss of quality* — decisions still run on full Sonnet when warranted.

## Anti-Pattern

**Wrong: Fixed cadence regardless of state**
```text
Every 30 min, always spawn gptme:
  - Check signals (0 found)
  - Check inbox (empty)
  - Write journal (nothing new)
  - Commit ("S220 — no signals")
  [Repeat 16x/day, every day]
```

**Correct: Gate on external conditions**
```text
Every 30 min, run lightweight check:
  - Signals? No. Messages? No. Imminent settlement? No.
  → Skip session, log "no signals"

(Different check run):
  - Signals? YES — BTC edge 7.2%, near strike
  → Spawn gptme with full Sonnet session
  → Trade decision, commit meaningful output
```

## Why Not a Lighter Model?

Using Haiku (or another small model) for "routine" sessions is tempting but problematic:
- **Tool use reliability**: Small models are unreliable at multi-step tool calls
- **Failure modes**: A confused Haiku session can corrupt state, create bad commits, or send wrong signals
- **Cost isn't that different**: 12 Haiku sessions still costs something; 0 sessions costs nothing
- **Complexity**: Managing per-session model routing adds orchestration complexity

Skip entirely rather than run cheaply.

## Outcome
Following this pattern results in:
- **Significant cost reduction**: 50-75%+ on inference spend for monitoring-heavy agents
- **Higher reliability**: No small-model tool-use failures on routine tasks
- **Cleaner signal**: Sessions that *do* run represent real decisions, not noise
- **Adaptive cadence**: Resource use tracks actual activity, not a fixed schedule

## Scope: Event-Gating Only

This lesson addresses one question: **when to skip a session entirely** because there's nothing to act on.

It does *not* address what to do with remaining inference budget when the agent has capacity left after covering event-driven work. When an agent has leftover cycles within its budget, it should still do productive autonomous work — exploring new opportunities, self-review, refining systems, running CASCADE sessions. Reducing an agent to a "position monitor" wastes available capacity.

The complementary pattern is **budget-based productive-work scheduling**: use CASCADE + Thompson sampling to pick the highest-value work category when the agent has inference budget and no immediate events to respond to. Session gating and productive-work scheduling are two separate mechanisms that should both be in place.

## Related
- [Blocked Period Status Check Trap](./blocked-period-status-check-trap.md) - Avoiding no-op commits
- [Strategic Focus During Autonomous Sessions](./strategic-focus-during-autonomous-sessions.md) - Prioritizing work

## Origin
2026-03-05: Extracted from analysis of a price-monitoring trading agent running 16 sessions/day with ~12-14 producing zero signals. Initial recommendation was model-tier switching (Sonnet for decisions, Haiku for routine), but revised after feedback that Haiku is too unreliable for tool use — the correct pattern is to skip sessions entirely using external rule-based gating, which is already how gptme's autonomous and project monitoring runs work.
