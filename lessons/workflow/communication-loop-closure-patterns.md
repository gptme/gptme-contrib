---
match:
  keywords:
    - "communication loop"
    - "github issue"
    - "memory failure"
    - "response"
    - "follow-through"
    - "request completion"
---

# Communication Loop Closure Patterns

## Purpose
Comprehensive patterns and templates for properly closing communication loops when completing requested actions, preventing the "memory failure" pattern of silent work completion.

## Core Principle
**Every completed action requires a response in the original request location.**

The communication loop is only closed when the requestor receives confirmation that their request was completed, with sufficient detail to verify the outcome.

## Communication Loop Types & Patterns

### Type 1: GitHub Issue Requests

**Scenario**: User requests action in GitHub issue comment or assignment

**Pattern**:
```markdown
## ✅ [Action] Completed as Requested

**Request**: [Brief summary of original request]
**Completed**: [Specific action taken]
**Output**: [Links to created items, code changes, or results]
**Next Steps**: [Any follow-up actions needed or recommendations]

-- [Your Name] 🔧

[Optional: Details section with additional context]
```

**Examples**:

*Good - Complete Response*:
```markdown
## ✅ Issue Created as Requested

**Request**: Create issue for imagen plugin improvements (from your comment on Dec 24)
**Completed**: Created comprehensive issue with 4 specific improvement areas
**Output**: Issue #83 - "Imagen Plugin Phase 3 Enhancements" (link)
**Next Steps**: Ready for your prioritization and technical review

-- Alice 🔧

### Details
- Variation generation system for iterative design
- Better prompt enhancement capabilities
- Config file support for persistent settings
- Performance optimizations for batch generation
```

*Bad - Silent Completion*:
```markdown
[No response at all - user doesn't know work was completed]
```

### Type 2: Chat/Direct Message Requests

**Scenario**: Request made in direct conversation or chat

**Pattern**:
```markdown
✅ **[Action] Done**

[Brief description of completion + link/details]

[Any important notes or next steps]
```

**Example**:
```markdown
✅ **Research completed**

Found 3 solid approaches to the context compression problem. Documented findings in knowledge/research/context-compression-analysis.md with pros/cons for each.

Key takeaway: ACE (Agentic Context Engineering) looks most promising for our use case.
```

### Type 3: Task Assignment/Delegation

**Scenario**: Assigned specific task or asked to take ownership of work

**Pattern**:
```markdown
## ✅ Task Completed: [Task Name]

**Status**: Completed
**Duration**: [Time spent]
**Outcome**: [Results achieved]
**Deliverables**: [Links to files, commits, or outputs]
**Learnings**: [Key insights or lessons learned]

Ready for your review/feedback.
```

### Type 4: Cross-Agent Communication

**Scenario**: Request from another agent for coordination or collaboration

**Pattern**:
```markdown
✅ **Request fulfilled** @[requesting-agent]

[Details of what was completed]

This should address your [specific need]. Let me know if you need any adjustments or additional details.
```

## Response Timing Guidelines

### Immediate Response Required (<1 hour):
- Blocking requests (marked urgent or blocking)
- Simple confirmations or status updates
- Requests from primary stakeholders (Erik for Alice)

### Same-Session Response (<4 hours):
- Standard action completion
- Research or analysis requests
- Issue creation or modification requests

### Next-Session Response (<24 hours):
- Complex implementations requiring multiple sessions
- Requests requiring coordination with others
- Non-urgent enhancement requests

**Never acceptable**: Response delays >48 hours without communication

## Response Quality Levels

### Minimal Acceptable Response:
```markdown
✅ Done: [link to result]
```

### Standard Professional Response:
```markdown
## ✅ [Action] Completed

**Request**: [Summary]
**Result**: [Link + brief description]
**Status**: Ready for review

-- [Name]
```

### Comprehensive Response (for important requests):
```markdown
## ✅ [Action] Completed Successfully

**Original Request**: [Detailed summary showing understanding]
**Actions Taken**:
- [Step 1 with results]
- [Step 2 with results]
- [Step 3 with results]

**Deliverables**:
- [Primary output with link]
- [Secondary outputs if any]
- [Documentation created]

**Quality Assurance**:
- [Verification steps taken]
- [Testing performed]
- [Review completed]

**Next Steps/Recommendations**:
- [Suggested follow-up actions]
- [Dependencies or blockers identified]
- [Future considerations]

**Timeline**: Completed in [duration] over [sessions]

Available for immediate follow-up questions or refinements.

-- [Name] 🔧
```

## Special Situations

### Emergency/Urgent Responses
When immediate response is needed but work isn't complete:

```markdown
🚨 **Urgent Response**: Working on your request

**Status**: In progress (started [time])
**ETA**: [Realistic completion estimate]
**Current Progress**: [Brief update]

Will provide full completion response when finished.
```

### Partial Completion
When work is partially complete but session ending:

```markdown
⏸️ **Partial Completion Update**

**Request**: [Original request]
**Progress**: [What's been completed]
**Remaining**: [What's left to do]
**ETA**: [When you'll continue]

Will provide final completion response when fully finished.
```

### Request Clarification Needed
When request isn't clear enough to complete:

```markdown
❓ **Clarification Needed**

**Request**: [Your understanding of request]
**Question**: [Specific clarification needed]
**Options**: [Possible interpretations if applicable]

Once clarified, I'll complete this and provide full response.
```

## Anti-Patterns to Avoid

❌ **Silent Completion**: Complete work without any response
❌ **Wrong Channel**: Respond in different location than request
❌ **Vague Response**: "Done" without details, links, or context
❌ **Assumption Response**: Assuming requestor will find completed work
❌ **Delayed Response**: Waiting days to confirm completion
❌ **Incomplete Response**: Missing key details like links or next steps
❌ **One-Word Response**: "✅" without any explanation

## Integration with Other Systems

### Work Queue Integration
```markdown
## Pending Responses Tracking
- [ ] Issue #4 (Erik): Visual identity analysis → Need comprehensive response
- [ ] Bob request: Research question → Share findings
- [x] User question: Bug investigation → ✅ Responded with solution
```

### Session End Checklist
```markdown
## Session End Communication Verification
- [ ] Did I complete any requested actions today?
- [ ] Have I responded to ALL requestors in original locations?
- [ ] Are my responses complete with links and next steps?
- [ ] Did I add any new pending responses to tracking?
```

### Git Commit Integration
Include response completion in commit messages:
```bash
git commit -m "docs(research): complete context analysis, respond to Erik's request in Issue #4"
```

## Success Metrics

- **Response Rate**: 100% of completed actions get follow-up responses
- **Response Quality**: All responses include links and sufficient detail
- **Response Time**: 95% within same session, 100% within 24 hours
- **Requestor Satisfaction**: No "did you see my request?" follow-ups
- **Communication Clarity**: Zero confusion about completion status

## Related Systems

This pattern integrates with:
- [Session Startup Recent Actions Review](./session-startup-recent-actions-review.md)
- [Pre-Issue Creation Checklist](./pre-issue-creation-checklist.md)
- [Memory Failure Prevention](./memory-failure-prevention.md)

## Real-World Application

**Before (Memory Failure Pattern)**:
1. Erik requests action in Issue #4
2. Agent creates Issue #83
3. Agent ends session without responding
4. Erik doesn't know request was completed
5. Potential duplicate work in future sessions

**After (Proper Closure Pattern)**:
1. Erik requests action in Issue #4
2. Agent creates Issue #83
3. Agent immediately responds in Issue #4 with completion confirmation
4. Erik sees request was fulfilled with links to results
5. Clear completion, no duplicates, professional communication

This pattern transformation addresses Erik's identified "memory failure" and ensures all future agent work maintains proper communication standards.
