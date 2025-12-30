---
match:
  keywords:
  - autonomous
  - session
  - pivot
  - blocked
  - troubleshooting
  - time management
  - alternative work
  - technical issues
  - value creation
  - autonomous mode
---

# Autonomous Session Pivot Strategies

## Rule
When technical issues block planned work in autonomous mode, pivot quickly to alternative value-creation rather than extended troubleshooting.

## Context
When running autonomous sessions with limited time and encountering technical blockers that prevent accessing planned work.

## Detection
Observable signals that you need to pivot:
- Spent 5-10+ minutes on same technical issue without progress
- Multiple approaches to same problem have failed (2-3 attempts)
- Technical issue is outside your control (API errors, service unavailable)
- Time remaining in session is limited
- Alternative high-value work is clearly available

Common patterns:
- GitHub API/CLI issues preventing issue access
- Network/service outages blocking planned research
- Missing tools/permissions preventing planned development work
- Authentication or access problems with external services

## Pattern

**Quick Pivot Sequence (5-10 minutes max)**:

1. **Try 2-3 Different Approaches** (3-5 minutes):
   ```shell
   # Example: GitHub issue access blocked
   gh issue view 6              # Primary approach fails
   gh issue view 6 --comments   # Secondary approach fails
   curl -s https://api.github.com/repos/owner/repo/issues/6  # Alternative fails
   ```

2. **Identify Alternative Value-Creation** (2-3 minutes):
   - Check completed tasks that could be upgraded/improved
   - Look for workspace improvements based on recent insights
   - Consider documentation or knowledge capture work
   - Review recent work for follow-up opportunities

3. **Pivot Decisively** (immediate):
   ```shell
   # Don't: Continue troubleshooting for 20+ minutes
   # Do: Acknowledge block and switch to valuable alternative

   # Example pivot from blocked GitHub work to workspace improvement:
   echo "GitHub issue #6 inaccessible due to API errors. Pivoting to high-value workspace architecture upgrade based on recent collaborative analysis."
   ```

## Anti-Pattern: Extended Troubleshooting
```bash
# Wrong: 20+ minutes on same technical issue
# Try approach 1... fails
# Try approach 2... fails
# Try approach 3... fails
# Research issue online... 10 minutes
# Try approach 4... fails
# Most of autonomous session consumed by single technical block

# Correct: Quick attempts then pivot
# Try 2-3 approaches (5 minutes)
# Identify alternative high-value work (2 minutes)
# Pivot to alternative work (remaining time productive)
```

## Alternative Value-Creation Strategies

**When blocked from planned work**:
- **Task System**: Update completed tasks, create new priority tasks
- **Knowledge Capture**: Document recent insights, create lessons learned
- **Workspace Improvements**: Implement architecture upgrades, organize files
- **Documentation**: Create analysis documents, improve existing docs
- **Research & Planning**: Investigate future work opportunities

## Example Successful Pivot

**Scenario**: GitHub issue #6 inaccessible due to API errors

**Failed Attempts** (5 minutes):
- `gh issue view 6` → GraphQL deprecation error
- `gh issue view 6 --comments` → Same error
- `curl` GitHub API → 404 error

**Successful Pivot** (remaining session time):
- Recognized all tasks marked as complete → updated task statuses
- Identified workspace architecture upgrade opportunity from recent collaborative analysis
- Created and activated new high-priority task
- Made concrete progress on lessons system improvement

**Outcome**: Session remained productive despite initial technical block

## Outcome
Following this pattern results in:
- **Time efficiency**: 90%+ of session time spent on valuable work
- **Consistent progress**: Technical issues don't derail entire sessions
- **Adaptive capability**: Flexible response to changing conditions
- **Value maximization**: Alternative work often equally or more valuable

## Related
- [Simplify Before Optimize](../patterns/simplify-before-optimize.md) - Don't over-optimize troubleshooting
- [Shell Output Filtering](../tools/shell-output-filtering.md) - Technical efficiency patterns