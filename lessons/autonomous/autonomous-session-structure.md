---
match:
  keywords:
  - autonomous
  - session
  - structure
  - phases
  - time management
  - workflow
  - completion
  - git status
  - commit
  - planning
  - autonomous mode
---

# Autonomous Session Structure

## Rule
Use structured 4-phase approach for autonomous sessions to maximize value creation and ensure proper work completion.

## Context
When operating in autonomous mode with limited time (typically 25-35 minutes) to complete meaningful work.

## Detection
Observable signals that structured approach is needed:
- Autonomous session starting without clear plan
- Previous sessions ended with uncommitted work
- Jumping directly to work without context gathering
- Sessions ending abruptly without proper completion
- Spending too much time on setup vs. actual progress

## Pattern

**4-Phase Autonomous Session Structure**:

### Phase 1: Quick Status Check (2-3 minutes)
```shell
# Check for uncommitted changes (critical first step)
git status

# If uncommitted changes exist, commit them first
git add specific-files && git commit -m "descriptive message"

# MEMORY FAILURE PREVENTION CHECK (CRITICAL)
# Check for incomplete communication loops from previous sessions
echo "=== MEMORY FAILURE PREVENTION CHECK ==="
gh issue list --author @me --limit 5 --json number,title,url
echo "Recent issues created ↑ - Check if any need follow-up responses"

git log --oneline --since="2 days ago" | grep -i "issue\|create\|comment" | head -3
echo "Recent work ↑ - Verify all communication loops closed"

# Check for broken communication loops in recent work
grep -r "TODO.*respond\|NEED.*RESPOND\|pending.*response" journal/ tasks/ || echo "✅ No pending responses found"


# Check recent context
ls -la journal/ | tail -5
head -20 journal/most-recent-entry.md
```

**Purpose**: Understand current state, handle any incomplete work from previous sessions, **prevent memory failures by checking for incomplete communication loops**

**CRITICAL RULE**: If ANY incomplete communication loops are found → **FIX THEM IMMEDIATELY** before starting new work. This prevents cascade failures where memory gaps compound across sessions.

### Phase 2: Task Selection (3-5 minutes)
```shell
# Check external commitments (SECONDARY in CASCADE)
gh issue list --assignee @me --state open

# Check ready tasks - tasks with no blockers (TERTIARY in CASCADE)
# Use --json for machine-readable output in automated workflows
tasks ready --json | jq '.ready_tasks[] | "\(.priority): \(.name)"'

# Get recommended next task with reasoning
tasks next --json | jq '.next_task'

# Full status for context (human-readable overview)
tasks status --compact
```

**CASCADE Priority Order**:
1. **PRIMARY**: Work queue (state/queue-manual.md) - planned next items
2. **SECONDARY**: Direct requests - GitHub assignments, notifications, mentions
3. **TERTIARY**: Ready workspace tasks - `tasks.py ready` shows unblocked work

**Selection Rule**: Check all three sources. First unblocked work found gets executed.

**Purpose**: Identify highest-value work to focus session time on

### Phase 3: Work Execution (15-25 minutes)
- Make concrete progress on selected task
- Create or update journal entries documenting work
- Update task files with progress and status changes
- Focus on completable units of work within time constraint

**Purpose**: Create maximum value in available time

### Phase 4: Commit and Complete (2-3 minutes)
```shell
# COMMUNICATION LOOP CLOSURE CHECK (CRITICAL)
# If you completed any actions requested in issues/PRs, verify you responded back
echo "=== COMMUNICATION LOOP CLOSURE CHECK ==="
echo "Did I complete any requested actions that need follow-up responses? Check recent work:"
git log --oneline -5 | grep -i "issue\|create\|fix\|implement" || echo "✅ No recent action-completion work found"

# Stage only intended changes (never git add .)
git add specific-changed-files

# Commit with descriptive message
git commit -m "type(scope): description of changes"

# Push to origin (critical - don't leave unpushed work)
git push origin master
```

**Purpose**: Ensure work is preserved, accessible for future sessions, and all communication loops are closed

## Time Management Principles

**Phase Time Allocation**:
- Setup/Status: 15-20% (5-7 minutes)
- Core Work: 70-80% (20-25 minutes)
- Completion: 10-15% (3-5 minutes)

**Work Selection Criteria**:
- Can make meaningful progress in available time
- Has clear next steps or completion criteria
- Creates value even if session ends mid-task
- Builds on previous work rather than starting completely new areas

## Anti-Patterns

**Poor Session Structure**:
```bash
# Wrong: Jumping directly to work without context
# Start coding immediately without checking git status
# Miss uncommitted changes from previous session
# Spend 20 minutes before realizing work conflicts with existing changes

# Wrong: No proper completion
# Make progress on task
# Session ends without committing work
# Next session loses previous progress
```

**Better Session Structure**:
```bash
# Correct: Structured approach
# Phase 1: git status → commit any existing work → check context
# Phase 2: review tasks → select appropriate work
# Phase 3: focused progress → document in journal
# Phase 4: commit changes → push to origin
```

## Success Patterns

**Effective Work Selection**:
- **Incremental Progress**: Tasks that can advance even if not completed
- **Documentation Work**: Always completable within session time
- **Task Management**: Updating statuses, creating new tasks based on insights
- **Lesson Creation**: Capturing recent learning experiences

**Completion Indicators**:
- All changes committed and pushed
- Task status appropriately updated
- Journal entry created documenting session
- Clear handoff to next session in task files or journal

## Example Session Flow

**Phase 1** (3 min):
- `git status` → working tree clean
- Check recent journal entries → understand recent work context

**Phase 2** (4 min):
- `gh issue list --assignee @me` → found GitHub issue from collaborator (SECONDARY)
- `tasks ready --json` → 4 ready tasks, highest priority: agent-hosting-patterns
- `tasks next --json` → recommends agent-hosting-patterns with reasoning
- Decision: Work on GitHub issue (SECONDARY takes priority over TERTIARY)

**Phase 3** (22 min):
- Updated 2 task statuses from active → done
- Created new workspace architecture upgrade task
- Made progress on Phase 1 of new task (lesson system analysis)
- Created 2 new lessons documenting recent learning

**Phase 4** (4 min):
- Stage specific files: `git add tasks/ lessons/ journal/`
- Commit: `git commit -m "docs(workspace): complete tasks, start architecture upgrade with lesson creation"`
- Push: `git push origin master`

**Total Value Created**: Task system cleaned up, new priority work identified, concrete progress on workspace improvement, 2 new lessons created

## Outcome
Following this pattern results in:
- **Consistent Progress**: Every session creates documented value
- **Work Continuity**: Proper handoffs between sessions
- **Time Efficiency**: Structured approach maximizes work time
- **Quality Completion**: No lost work, proper documentation

## Related
- [Autonomous Session Pivot Strategies](./autonomous-session-pivot-strategies.md) - Handling technical blocks
- [Git Workflow](../workflow/git-workflow.md) - Proper commit and push practices
- [Inter-Agent Communication](../workflow/inter-agent-communication.md) - Coordination and escalation
