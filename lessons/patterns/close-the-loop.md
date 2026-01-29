---
match:
  keywords:
    # Specific problem triggers (high precision)
    - "reduce human intervention in autonomous workflows"
    - "make agents more autonomous"
    - "automate the feedback loop"
    - "autonomous long-running tasks"
    - "minimize human touchpoints"
    # Workflow triggers
    - "pre-commit hooks"
    - "greptile review"
    - "auto-merge"
    - "CI feedback"
    # General concepts
    - "close the loop"
    - "feedback loop"
    - "autonomous operation"
status: active
---

# Close the Loop

## Rule
Design workflows where agents can autonomously complete long-duration tasks by adding automated feedback mechanisms at each potential human intervention point.

## Context
When building autonomous agent workflows that currently require human intervention at multiple points.

## Detection
Observable signals that a loop needs closing:
- Agent completes work but waits indefinitely for human review
- Multiple round-trips between agent and human for simple fixes
- Manual steps that could be automated (linting, testing, reviewing)
- Agent cannot proceed without human approval on routine matters
- Long delays between "work done" and "work merged"

## Pattern

### Principle
Every point where an agent would stop and wait for human input is an opportunity to close the loop with automation.

### Common Loop Closures

**1. Pre-commit Hooks (Quality Gates)**
```text
Before: Agent → Commit → Human reviews style/quality → Fixes needed
After:  Agent → Pre-commit auto-fixes → Commit already clean
```
- Linting and formatting (ruff, black, prettier)
- Type checking (mypy, pyright)
- Security scanning (bandit, safety)
- Import sorting, trailing whitespace

**2. CI/CD Feedback (Build Validation)**
```text
Before: Agent → Push → Wait for human to check CI → Debug failures
After:  Agent → Push → Read CI logs → Auto-fix → Re-push
```
- Parse CI failure logs programmatically
- Apply common fixes (import errors, test failures)
- Re-run until green or escalate

**3. Automated Code Review (Quality Assurance)**
```text
Before: Agent → PR → Wait for human review → Address feedback
After:  Agent → PR → Greptile reviews → Fix issues → Re-request → 5/5 → Merge
```
- Use Greptile or similar for automated review
- Fix identified issues autonomously
- Request re-review until score threshold met
- Auto-merge when criteria satisfied

**4. Auto-merge (Completion)**
```text
Before: Agent → PR ready → Wait for human merge
After:  Agent → CI green + Review 5/5 → Auto-merge (for routine PRs)
```
Criteria for auto-merge:
- All CI checks passing
- Automated review score ≥ threshold (e.g., Greptile 5/5)
- Low-risk change category (docs, tests, small fixes)
- No design decisions requiring judgment

### The Complete Autonomous Loop

```text
┌─────────────────────────────────────────────────────────┐
│                 AUTONOMOUS WORK LOOP                     │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  1. Agent receives task                                  │
│        │                                                 │
│        ▼                                                 │
│  2. Write code                                           │
│        │                                                 │
│        ▼                                                 │
│  3. Pre-commit hooks ──────── Fix issues ◄───┐          │
│        │                          │           │          │
│        │ Pass                     └───────────┘          │
│        ▼                                                 │
│  4. Push to branch                                       │
│        │                                                 │
│        ▼                                                 │
│  5. CI runs ───────────────── Parse logs, fix ◄───┐     │
│        │                          │                │     │
│        │ Pass                     └────────────────┘     │
│        ▼                                                 │
│  6. Greptile reviews ──────── Address feedback ◄───┐    │
│        │                          │                 │    │
│        │ 5/5                      └─────────────────┘    │
│        ▼                                                 │
│  7. Auto-merge (if routine) ─────── Done!               │
│        │                                                 │
│        │ Complex                                         │
│        ▼                                                 │
│  8. Human review ─────────── Address feedback ◄───┐     │
│        │                          │                │     │
│        │ Approved                 └────────────────┘     │
│        ▼                                                 │
│  9. Merge ─────────────────────── Done!                 │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

### Implementation Checklist

When adding a new autonomous workflow, ask:
- [ ] What quality gates can run automatically? (pre-commit)
- [ ] How can the agent read and respond to CI feedback?
- [ ] Is automated code review configured?
- [ ] What criteria allow auto-merge vs require human review?
- [ ] How does the agent know when to escalate?

## Outcome
Following this pattern results in:
- **Reduced latency**: Hours of wait time → minutes of automation
- **Consistent quality**: Every commit passes same gates
- **Increased throughput**: More work completed per session
- **Human focus**: Reviewers handle only what requires judgment
- **Scalable autonomy**: Patterns compound as more loops close

## Anti-Patterns

**Premature escalation**: Asking humans for help before exhausting automated options.

**Open loops**: Starting work that requires human approval to complete, when autonomous completion was possible.

**Manual gates**: Requiring human approval for routine, low-risk changes.

## Related
- [Pre-commit Configuration](../../.pre-commit-config.yaml) - Quality gates example
- [Issue #282](https://github.com/ErikBjare/bob/issues/282) - Origin of this pattern
