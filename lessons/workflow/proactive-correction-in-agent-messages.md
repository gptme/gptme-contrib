---
status: active
tags: [inter-agent, correction, orchestration, epistemic-integrity, communication]
match:
  keywords:
    - correction
    - retract
    - wrong advice
    - incorrect recommendation
    - agent message
    - proactive correction
---

# Proactive Correction in Agent Messages

## Rule
When you realize you sent incorrect or incomplete advice to another agent, send a correction
immediately — before they act on it. Don't wait, don't hope they'll figure it out.

## Context
Multi-agent systems where agents act autonomously on received messages. The gap between
sending advice and an agent acting on it can be hours or multiple sessions.
If incorrect advice compounds before correction arrives, the recovery cost is higher.

## Detection
Observable signals that a correction is needed:
- You just sent a recommendation to an agent and subsequently realized part of it was wrong
- You discover that configuration, facts, or reasoning in a prior message were incorrect
- A new constraint or consideration emerged that invalidates prior advice
- Another agent or principal points out an error in your prior message

## Pattern

### 1. Send immediately via the same channel

Use the same channel as the original message. For GitHub issue-based communication
(per [inter-agent-communication.md](./inter-agent-communication.md)), comment on the
same issue or create a follow-up issue:

```bash
# Comment on the original issue (keeps context together)
gh issue comment <issue-number> --repo owner/agent-repo --body "$(cat <<'EOF'
**Correction to my earlier message:**

I wrote:
> [quote the incorrect advice]

**Retract that.** [One sentence explaining why it was wrong.]

**The correct approach**: [correct recommendation]

Steps 1-2 and 4-5 from the original still apply.
EOF
)"

# Or, for a larger correction, create a follow-up issue
gh issue create --repo owner/agent-repo \
  --title "Correction: [brief description]" \
  --body "..."
```

### 2. Structure the correction clearly

Good corrections include:
- **What to retract**: quote the specific wrong advice, verbatim if short
- **What still stands**: explicitly confirm the parts that were correct
- **Why the correction**: one sentence explaining what was wrong — this helps the agent understand, not just comply
- **Correct replacement**: the right advice/config/action to use instead

```markdown
I wrote:

> [quote the incorrect advice]

**Retract that.** [One sentence explaining why it was wrong.]

**The correct approach**: [correct recommendation]

Steps 1-2 and 4-5 from the original message still apply.
```

## Anti-Patterns

**Wrong: Hoping the agent figures it out**
Agents don't infer corrections. If you sent bad advice, the agent will act on it.

**Wrong: Sending correction days later**
The agent may have already acted. Send corrections within the same session if possible.

**Wrong: Vague retraction without explanation**
Saying "ignore my last message" without context leaves the agent uncertain about what
still applies. Always state what to retract, what to keep, and why.

**Wrong: Correcting only the wrong part without restating the full picture**
If the original had five steps and step 3 was wrong, restate the correct step 3
and confirm steps 1-2 and 4-5 still apply. Agents need the complete updated picture.

## Examples from Practice

**Case 1 — Model selection recommendation**:
An orchestrator sent a message recommending a smaller (cheaper) model as a fallback for
a monitoring agent. Realized small models are unreliable for multi-step tool use — they
can corrupt state or produce incorrect signals. Sent a correction retracting the model
recommendation and reinforcing that external session-count gating (no LLM spawn for
no-signal sessions) is the correct approach.

**Case 2 — Incomplete configuration**:
A message contained an incomplete config for an agent's settings, missing required fields.
The principal flagged the gap before the agent acted. Sent a correction with the full
config and a pointer to the canonical setup guide.

## Outcome

Following this pattern results in:
- **Lower compound error risk**: agents don't build on incorrect advice
- **Clear audit trail**: corrections are visible in the message thread
- **Trust**: agents and principals see that errors are caught and corrected quickly
- **Reduced recovery cost**: catching errors before they propagate is far cheaper

## Related

- [Inter-Agent Communication](./inter-agent-communication.md) — channel selection and message format

## Origin

2026-03-06: Two corrections sent in a single session — one retracting a model fallback
recommendation, one fixing an incomplete config. The pattern of "send correction immediately
via same channel" is distinct enough from general communication lessons to warrant its own entry.
