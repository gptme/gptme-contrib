---
match:
  keywords:
  - autonomous
  - work execution
  - structured approach
  - phases
  - productivity
  - task management
  - session
  - dependencies
  - priority
  - autonomous mode
  - planning
---

# Effective Autonomous Work Execution

## Rule
Follow a structured 4-phase approach for autonomous work sessions to maximize productivity and proper task management.

## Context
When conducting autonomous work sessions with multiple potential tasks and dependencies.

## Detection
Observable signals for using this pattern:
- Multiple tasks available with different priorities
- Some tasks blocked on external dependencies
- Need to make meaningful progress in limited time (25-30 min sessions)
- Mix of independent and dependent work available

## Pattern
**4-Phase Structured Approach:**

### Phase 1: Quick Status Check (2-3 min)
```shell
# Check git status and resolve any conflicts
git status

# Review recent journal context
ls -la journal/ | tail -5
cat journal/most-recent-entry.md
```

### Phase 2: Task Selection (3-5 min)
```shell
# Check task status and priorities
./scripts/tasks.py status --compact

# Check for GitHub notifications/issues
gh issue list --assignee @me

# Identify blocked vs independent work
# Create GitHub issues for blocked items
```

### Phase 3: Work Execution (15-25 min)
- **If major task blocked**: Document properly and find independent work
- **Update task status**: Reflect actual completion, not just estimates
- **Extract lessons**: Capture patterns from recent successful work
- **Make incremental progress**: Even small improvements add value

### Phase 4: Commit and Complete (2-3 min)
```shell
# Stage only intended files
git add specific-files.md

# Descriptive commit messages
git commit -m "docs(tasks): update completion status to reflect actual progress"

# Push to origin
git push origin master
```

## Anti-Patterns
- **Working on blocked tasks**: Spinning wheels waiting for external input
- **Not documenting blockers**: Letting blocked tasks languish without communication
- **Skipping status updates**: Task files not reflecting actual completion
- **Avoiding independent work**: Missing opportunities for incremental progress

## Success Example
2025-12-12 session:
- ✅ Identified 2 major tasks blocked on Erik's input
- ✅ Created GitHub issue #13 for quantified self data access
- ✅ Updated task completion from 72% to 83% based on actual work done
- ✅ Found productive independent work (lesson creation)
- ✅ All changes committed with proper messages

## Outcome
Following this pattern results in:
- **Clear progress tracking**: Tasks reflect reality
- **Unblocked workflow**: Dependencies properly escalated
- **Continuous improvement**: Always finding valuable work
- **Professional communication**: Stakeholders informed of blockers

## Related
- [Git Workflow](../workflow/git-workflow.md) - Commit practices
- [Inter-Agent Communication](../workflow/inter-agent-communication.md) - Escalating blockers

## Origin
2025-12-12: Extracted from successful autonomous work pattern where blocked major tasks were properly handled while finding productive independent work.
