---
category: communication
match:
  keywords:
    - issue follow-through
    - close communication loop
    - respond after completing
    - confirm completion in thread
    - missing follow-up response
    - broken communication loop
---

# GitHub Issue Follow-Through Pattern

## Rule
ALWAYS respond in the original issue thread when completing any action requested in that thread.

## Context
When someone requests an action in a GitHub issue and you take that action (create another issue, write code, etc.), you must circle back to confirm completion in the original conversation.

## Detection
Observable signals that you need this pattern:
- Someone asked you to "create an issue about X" in an issue thread
- You've completed a task that was requested in a GitHub conversation
- You took an action but didn't confirm completion back where it was requested
- Multiple sessions later, you're unaware of actions you previously took
- Creating duplicate issues because you lost track of what was already done

## Pattern
Complete the feedback loop EVERY TIME:

```markdown
## ✅ [Action] Completed

I've completed the requested [action]:

**[Specific deliverable]**: [Link to what you created]
**Status**: [Brief status or outcome]
**Key [details]**: [Important specifics]

[Brief summary of what was accomplished]

---

**Ready for next steps**: [What's ready now or what you're waiting for]
```

**Critical Elements:**
1. **Respond WHERE you were asked** - not just somewhere else
2. **Link to what you created** - make it easy to find
3. **Confirm completion clearly** - use checkmarks and clear status
4. **Brief summary** - don't make them hunt for details
5. **Signal next steps** - show you're tracking the full context

## Anti-Pattern Examples

**❌ Wrong - Silent completion:**
- Create the requested issue but never respond back
- Complete work but only document it in your own notes
- Take action but leave original requester wondering if it's done

**❌ Wrong - Indirect confirmation:**
- Mention completion in a different issue
- Only document in your personal journal
- Assume they'll discover it on their own

## Outcome
Following this pattern prevents:
- **Duplicate work**: Others don't re-request completed actions
- **Memory failure**: Clear record of what was done when
- **Communication breakdown**: Requesters know their requests were handled
- **Context loss**: Full conversation thread maintains continuity

Following this pattern enables:
- **Trust building**: Demonstrates reliable follow-through
- **Efficient collaboration**: Clear communication loop completion
- **Better coordination**: Everyone knows what's been done
- **Context preservation**: Future sessions can see completed work

## Example Success Pattern

```markdown
# Example Request
Erik: "Create an issue about improving the imagen plugin"

# Example Proper Response (same thread)
## ✅ Imagen Plugin Improvement Issue Created

I've created the requested issue for imagen plugin enhancement:

**Issue #83**: https://github.com/gptme/gptme-contrib/issues/83 - "Add image modification support"
**Status**: Closed as duplicate (feature already in backlog)
**Key Feature**: Image + text modification prompts for iterative design

This addresses the "nano banana" functionality you mentioned for faster avatar iteration.

**Ready for next steps**: Feature is now properly tracked in the backlog.
```

## Recovery Pattern
If you discover you failed to follow through:
1. **Immediately respond** in the original thread
2. **Acknowledge the gap**: "I completed this but failed to follow up"
3. **Provide the missing information**: Links, status, outcomes
4. **Update lessons**: Reinforce the pattern to prevent repetition

## Related
- [Inter-Agent Communication](../workflow/inter-agent-communication.md) - Cross-agent coordination
- [GitHub Comment Formatting](./github-comment-formatting.md) - Proper link formatting

## Origin
Created 2025-12-24 after identifying "memory failure" pattern where actions were completed but follow-through responses were missing, leading to duplicate work and broken communication loops.
