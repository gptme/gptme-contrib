# Communication Loop Closure Patterns — Companion Doc

Full implementation details for the primary lesson at `lessons/workflow/communication-loop-closure-patterns.md`.

## Response Templates by Channel

### GitHub Issue Requests

```markdown
## ✅ [Action] Completed as Requested

**Request**: [Brief summary of original request]
**Completed**: [Specific action taken]
**Output**: [Links to created items, code changes, or results]
**Next Steps**: [Any follow-up actions needed or recommendations]
```

### Chat/Direct Message Requests

```markdown
✅ **[Action] Done**

[Brief description of completion + link/details]
[Any important notes or next steps]
```

### Task Assignment/Delegation

```markdown
## ✅ Task Completed: [Task Name]

**Status**: Completed
**Outcome**: [Results achieved]
**Deliverables**: [Links to files, commits, or outputs]

Ready for review/feedback.
```

### Cross-Agent Communication

```markdown
✅ **Request fulfilled** @[requesting-agent]

[Details of what was completed]
This should address your [specific need].
```

## Response Timing Guidelines

| Priority | Timeframe | Examples |
|----------|-----------|---------|
| Immediate | <1 hour | Blocking requests, primary stakeholders |
| Same-session | <4 hours | Standard completions, research requests |
| Next-session | <24 hours | Complex implementations, coordination needed |

Never acceptable: >48 hours without communication.

## Response Quality Levels

**Minimal**: `✅ Done: [link to result]`

**Standard**:
```markdown
## ✅ [Action] Completed

**Request**: [Summary]
**Result**: [Link + brief description]
**Status**: Ready for review
```

**Comprehensive** (for important requests):
```markdown
## ✅ [Action] Completed Successfully

**Original Request**: [Detailed summary]
**Actions Taken**: [Step-by-step with results]
**Deliverables**: [Links to outputs]
**Quality Assurance**: [Verification performed]
**Next Steps**: [Recommendations]
```

## Special Situations

### Partial Completion (session ending)

```markdown
⏸️ **Partial Completion Update**

**Request**: [Original request]
**Progress**: [What's been completed]
**Remaining**: [What's left to do]
**ETA**: [When you'll continue]
```

### Request Clarification Needed

```markdown
❓ **Clarification Needed**

**Request**: [Your understanding]
**Question**: [Specific clarification needed]
**Options**: [Possible interpretations if applicable]
```

## Anti-Patterns

- **Silent Completion**: Complete work without any response
- **Wrong Channel**: Respond in a different location than the request
- **Vague Response**: "Done" without details, links, or context
- **Assumption Response**: Assuming requestor will find completed work
- **Delayed Response**: Waiting days to confirm completion

## Session End Checklist

At the end of each session:
1. Did I complete any requested actions?
2. Have I responded to ALL requestors in original locations?
3. Are my responses complete with links and next steps?

## Real-World Example

**Before (Memory Failure Pattern)**:
1. Erik requests action in Issue #4
2. Agent creates Issue #83
3. Agent ends session without responding
4. Erik doesn't know request was completed → potential duplicate work

**After (Proper Closure Pattern)**:
1. Erik requests action in Issue #4
2. Agent creates Issue #83
3. Agent immediately responds in Issue #4 with completion confirmation
4. Erik sees request was fulfilled with links to results
