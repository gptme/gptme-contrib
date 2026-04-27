---
match:
  keywords:
  - close the loop
  - update the issue
  - reply to the issue
  - comment on the issue
  - follow up on the request
  session_categories: [triage, cross-repo]
status: active
---

# Communication Loop Closure Patterns

## Rule
Every completed action requires a response in the original request location. The loop is only closed when the requestor receives confirmation with enough detail to verify the outcome.

## Context
When completing work requested through GitHub issues, chat messages, task assignments, or cross-agent communication. Prevents the "memory failure" pattern where work is done silently.

## Detection
- Completing work requested in a GitHub issue without commenting back
- Ending a session without confirming completed requests
- Responding in a different location than where the request was made
- Vague responses like "Done" without links, details, or next steps

## Pattern
```markdown
## Completed: [Action]

**Request**: [Brief summary]
**Result**: [Link + description]
**Next steps**: [Follow-up if any]
```

Anti-patterns:
- Silent completion (no response at all)
- Wrong channel (respond somewhere other than where asked)
- Vague response (no links, no details)

## Outcome
- Requestors know their request was fulfilled
- No duplicate work from unacknowledged completions
- Clear audit trail in the original request location

## Related
- Companion doc: [knowledge/lessons/workflow/communication-loop-closure-patterns.md](../../knowledge/lessons/workflow/communication-loop-closure-patterns.md)
- [Session Startup Recent Actions Review](./session-startup-recent-actions-review.md)
- [Memory Failure Prevention](./memory-failure-prevention.md)
