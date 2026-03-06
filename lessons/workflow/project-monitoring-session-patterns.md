---
match:
  keywords:
    - project monitoring
    - monitoring session
    - github notifications
    - notification triage
    - multi-project
status: active
---

# Project Monitoring Session Patterns

## Rule
Use systematic classification and time-boxed execution for efficient project monitoring sessions that handle multiple ongoing commitments without derailing primary work.

## Context
When managing multiple active projects, PRs, and notifications that require periodic monitoring and maintenance work rather than primary creative/strategic development.

## Detection
Observable signals that you need a structured monitoring session approach:
- Multiple GitHub notifications requiring triage and action
- Active PRs or issues in various repositories needing attention
- Mix of actionable items and "monitoring only" status updates
- Need to maintain multiple project commitments efficiently
- Risk of spending entire session on maintenance vs. primary work

## Pattern
Implement a systematic 4-phase monitoring workflow:

### Phase 1: Investigation and Context Gathering (3 minutes)
```bash
# Systematic notification review
gh api notifications --jq '.[] | select(.unread == true)'
# Quick context reading for each item
# Focus on understanding scope, not deep investigation
```

### Phase 2: Classification — GREEN vs RED Work (2 minutes)
**RED (Action Required):**
- CI failures blocking PRs you created
- Critical bugs in your contributions
- Communication loops requiring closure
- Time-sensitive responses needed

**GREEN (Monitoring Only):**
- PRs under review (no action needed from you)
- Issues assigned to others (observation only)
- Successfully merged/completed work
- Work awaiting external dependencies

### Phase 3: Focused Execution (7 minutes)
```bash
# Handle RED items systematically:
# 1. Fix critical blockers first (CI failures, bugs)
# 2. Close communication loops (PR comments, status updates)
# 3. Quick wins that unblock others
# 4. Defer complex work to dedicated sessions
```

### Phase 4: Communication and Documentation (2–3 minutes)
```bash
# Close loops and document outcomes
# Update relevant stakeholders
# Document what's resolved vs. what needs follow-up
# Brief journal entry for session tracking
```

## Success Example

**Investigation Phase (3 min):**
- Reviewed PR status and CI failure details
- Checked issue and sibling PR status across repositories
- Identified specific blocker: missing YAML frontmatter

**Classification Phase (2 min):**
- **RED**: PR CI failure (action required)
- **GREEN**: Open issue (under review, monitoring only)
- **GREEN**: Sibling PR (already merged, no action needed)

**Execution Phase (7 min):**
- Fixed critical CI blocker: added YAML frontmatter to 2 files
- Committed, pushed, and updated PR with status comment
- Verified other items require no immediate action

**Documentation Phase (2 min):**
- Updated PR with resolution status
- Brief session summary documenting outcomes

**Result**: 12-minute session resolved a critical blocker while maintaining awareness of all other projects.

## Anti-Patterns

**Wrong: Investigation rabbit holes**
```text
# Spending 20+ minutes researching each notification
# Deep diving into context when a quick overview is sufficient
# Reading entire PR history when current status is enough
# Result: Session consumed by investigation, no execution
```

**Wrong: No classification system**
```text
# Working on the first notification encountered
# Mixing monitoring work with action items randomly
# No priority distinction between critical and nice-to-have
# Result: Critical blockers missed while time is spent on non-urgent items
```

**Correct: Systematic triage and focused execution**
```text
# Quick context scan across all items (3 min)
# Clear RED/GREEN classification (2 min)
# Focused execution on RED items only (7 min)
# Result: Critical work completed, all projects properly monitored
```

## Time Management Principles

**Investigation Discipline:**
- 3 minutes max for context gathering across ALL items
- Quick read sufficient for classification
- Deep investigation only for RED items during execution

**Classification Efficiency:**
- Binary decision: Action Required vs. Monitoring Only
- Focus on impact: what blocks others vs. what's just awareness
- Default to GREEN unless clear RED criteria are met

**Execution Focus:**
- RED items only during monitoring sessions
- Complex work deferred to dedicated sessions
- Communication loops closed immediately after fixes

## Outcome
Following this pattern results in:
- **Maintained Project Health**: Critical blockers resolved quickly
- **Efficient Time Use**: 12–15 minute sessions handle multiple projects
- **Clear Priorities**: Action items distinguished from monitoring items
- **Professional Communication**: Stakeholders updated on progress/resolution
- **Protected Primary Work**: Monitoring doesn't derail creative/strategic sessions

Benefits for multi-project management:
- Systematic approach prevents important items from being missed
- Time-boxed structure prevents monitoring from consuming entire sessions
- Clear classification enables efficient triage decisions
- Communication discipline maintains professional project relationships

## Related
- [Autonomous Session Structure](../autonomous/autonomous-session-structure.md) — General session management
- [Memory Failure Prevention](./memory-failure-prevention.md) — Ensuring communication loops are closed
- [Communication Loop Closure Patterns](./communication-loop-closure-patterns.md) — Stakeholder communication follow-through

## Origin
Extracted from a successful 12-minute PR monitoring session demonstrating efficient multi-project monitoring with systematic classification, focused execution, and proper communication loop closure.
