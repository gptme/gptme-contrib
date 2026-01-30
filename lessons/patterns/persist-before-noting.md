---
match:
  keywords:
    - "noted for future reference"
    - "I'll remember this"
    - "will keep this in mind"
    - "good to know for next time"
    - "learned this for future"
    - "understanding improved"
status: active
---

# Persist Before Noting

## Rule
When you say you've "noted" or "learned" something, immediately persist it to a core file before continuing.

## Context
During conversations when valuable insights, clarifications, or corrections are discovered.

## Detection
Observable signals that indicate empty promises:
- Saying "noted for future reference" without updating files
- Using phrases like "I'll remember this" without persistence
- Claiming "understanding improved" but no visible changes
- Responding to corrections with acknowledgment only
- Using "good to know" without creating a lesson or updating docs

Common anti-patterns:
```text
User: Actually, X and Y are different things, don't confuse them.
Agent: "Thanks for the clarification! Noted for future reference."
# No file was updated - this insight will be lost!
```

## Pattern
When something is worth noting, persist it immediately:

```text
# ❌ Wrong: Empty acknowledgment
User: FYI, Claude Code tasks ≠ beads/bd
Agent: "Noted for future reference."
# Nothing persisted, will forget next session

# ✅ Correct: Persist then acknowledge
User: FYI, Claude Code tasks ≠ beads/bd
Agent: "Let me persist this to prevent future confusion."
[Updates GLOSSARY.md with distinction]
[Commits changes]
Agent: "Added to glossary. Now I'll apply this understanding."
```

**Where to persist**:
- **Terminology/concepts**: GLOSSARY.md
- **Behavioral patterns**: lessons/ directory
- **Coding preferences**: knowledge/agent/programming-style-guide.md
- **Tool usage**: lessons/tools/
- **Process improvements**: knowledge/processes/

## Outcome
Following this pattern ensures:
- **Compound learning**: Insights persist across sessions
- **No rediscovery**: Same mistake won't happen again
- **Audit trail**: Git history shows when you learned something
- **Concrete progress**: Visible changes vs empty words
- **Trust building**: Actions match stated intentions

## Related
- Agent workspaces typically have GLOSSARY.md for terminology
- Agent lessons/ directories for behavioral patterns
- knowledge/ directories for detailed documentation
