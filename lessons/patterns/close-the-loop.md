---
match:
  keywords:
    # Specific problem triggers (high precision)
    - "reduce human intervention in autonomous workflows"
    - "make agents more autonomous"
    - "automate the feedback loop"
    - "autonomous long-running tasks"
    - "minimize human touchpoints"
    # General concepts
    - "close the loop"
    - "feedback loop"
    - "autonomous operation"
    - "waiting for human input"
    - "human bottleneck"
status: active
---

# Close the Loop

## Rule
Every point where an agent would stop and wait for human input is an opportunity for automation.

## Context
When building or improving autonomous agent workflows that currently require human intervention.

## Detection
Observable signals that a loop needs closing:
- Agent completes work but waits indefinitely for next step
- Multiple round-trips between agent and human for routine matters
- Agent cannot proceed without human approval on predictable/automatable decisions
- Workflow has manual steps that produce consistent outcomes
- Human intervention point has clear success/failure criteria

## Pattern

### Core Principle
Identify every "handoff to human" point in a workflow. For each, ask:
1. **Is it predictable?** Does the human always make the same decision given the same inputs?
2. **Is it measurable?** Can success/failure be determined programmatically?
3. **Is it low-risk?** Would automation failures be recoverable?

If yes to all three, close the loop with automation.

### The Pattern

```text
BEFORE (Open Loop):
  Agent → Work → Stop → Human decides → Agent continues → Stop → Human decides → ...

AFTER (Closed Loop):
  Agent → Work → Automated check → Continue/Fix → ... → Human only for judgment calls
```

### Closing Techniques

| Human Intervention | Automation Approach |
|-------------------|---------------------|
| "Is the output correct?" | Automated validation/testing |
| "Should this proceed?" | Rules + criteria → auto-approve or escalate |
| "What's wrong?" | Parse error messages → auto-fix common issues |
| "Is this ready?" | Checklists → programmatic verification |

### Implementation Questions

When designing autonomous workflows, ask for each step:
- [ ] Can the agent verify its own success?
- [ ] Can failure modes be detected and handled?
- [ ] What criteria distinguish "auto-proceed" from "needs human"?
- [ ] How does the agent know when to escalate?

### Escalation Threshold

Not all loops should be closed. Escalate to humans when:
- Decision requires judgment or domain expertise
- Stakes are high (irreversible, costly, public)
- Novel situation outside defined criteria
- Multiple failed auto-fix attempts

## Outcome
Following this pattern results in:
- **Reduced latency**: Wait times → automation cycles
- **Increased throughput**: More work per session
- **Human focus**: Reviewers handle only what requires judgment
- **Scalable autonomy**: Each closed loop compounds capability

## Anti-Patterns

**Premature escalation**: Asking humans before exhausting automated options.

**Open loops**: Starting work requiring human approval when autonomous completion was possible.

**Over-automation**: Closing loops on high-stakes decisions that genuinely need human judgment.

## Examples

The principle applies broadly. See these lessons for specific implementations:

**Quality Gates (Code quality before human review)**:
- [Greptile PR Reviews](../tools/greptile-pr-reviews.md) - Automated code review: fix issues → re-trigger review → achieve 5/5 → proceed

**Communication Loops (Complete feedback cycles)**:
- [Communication Loop Closure](../workflow/communication-loop-closure-patterns.md) - Every completed action gets a response in the original location
- [Memory Failure Prevention](../workflow/memory-failure-prevention.md) - Check for incomplete loops at session start

**Development Workflow (CI/Pre-commit automation)**:
- [Git Workflow](../workflow/git-workflow.md) - Pre-commit hooks auto-fix before humans see issues
- [Git Worktree Workflow](../workflow/git-worktree-workflow.md) - Includes pre-commit setup for quality gates

**Session Structure (Autonomous completion)**:
- [Autonomous Session Structure](../autonomous/autonomous-session-structure.md) - 4-phase approach with built-in completion verification

## Related
- [Issue #282](https://github.com/ErikBjare/bob/issues/282) - Origin of this pattern
