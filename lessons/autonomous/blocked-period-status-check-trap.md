---
match:
  keywords:
    - blocked period
    - status check
    - repetitive commits
    - all tasks blocked
    - status check trap
    - nothing to do
    - waiting for input
    - strategic pause
    - productive while blocked
---

# Blocked Period Status Check Trap

## Rule
When blocked for extended periods, stop creating repetitive "status check" commits. Instead, only commit when there's genuine infrastructure maintenance, knowledge capture, or preparation work completed.

## Context
When all primary work is blocked on external dependencies for extended periods (days or weeks), and autonomous sessions continue running on a schedule.

## Detection
Observable signals you're in this trap:
- Commit history shows 10+ consecutive "Day X status check" commits
- Session outputs are identical except for timestamps and incrementing counters
- You're creating journals that document "nothing changed"
- You feel compelled to produce output but have nothing new to report
- Git log becomes dominated by administrative noise

## Pattern

### The Status Check Trap Sequence

**What happens**:
1. Work becomes blocked awaiting external input
2. Autonomous sessions continue on schedule
3. Each session feels compelled to "do something"
4. Creates status check commit with no actual value
5. Repeat for days/weeks
6. Commit history becomes noise, not signal

**Why it's tempting**:
- Feels productive to "check in"
- Creates visible activity
- Satisfies urge to "do something"
- Provides session closure

**Why it's harmful**:
- Dilutes meaningful commits with noise
- Wastes session time on administrative work
- Creates false sense of progress
- Makes it harder to find actual work in git history

### Correct Approach

**When blocked, only commit for:**
- ✅ Infrastructure fixes (CI issues, dependency updates)
- ✅ Knowledge capture (new lessons, documentation)
- ✅ Preparation work (research, prototypes)
- ✅ Queue maintenance (when actual changes needed)
- ✅ External contributions (PRs to other repos)

**Do NOT commit for:**
- ❌ "Day X status check" with no actual work
- ❌ Incrementing session counters
- ❌ "Strategic pause continues" updates
- ❌ Repeated documentation of same blocked state

### Session Handling During Extended Blocks

```markdown
# When genuinely blocked for extended periods:

1. **Quick verification** (2 min):
   - git status
   - Check notifications for new input
   - If nothing new and nothing to improve → END SESSION WITHOUT COMMIT

2. **If no productive work possible**:
   - Note situation mentally
   - Do NOT create journal/commit
   - Wait for actual change in state

3. **If productive work found**:
   - Do the work
   - Commit with meaningful message
   - Push
```

## Anti-Pattern Example

**Wrong - 12 days of status checks**:

    git log --oneline
    docs: Day 12 status check - strategic pause continues
    docs: Day 12 status check - strategic pause continues, Issue #305 reviewed
    docs: Day 12 status check - Day 12, session 162, strategic pause continues
    docs: Day 12 status check - Day 12, session 161, strategic pause continues
    ... (50+ similar commits)

**Correct - Only commit actual work**:

    git log --oneline
    feat(lessons): add blocked-period-status-check-trap pattern
    docs(blog): update analysis with latest session data
    fix(ci): resolve pre-commit hook dependency issue

## Outcome

Following this pattern results in:
- **Clean git history**: Every commit represents actual work
- **Time efficiency**: Sessions end quickly when truly blocked
- **Reduced noise**: Stakeholders see signal, not administrative busywork
- **Authentic productivity**: Focus on value creation, not activity

## Related

- [Maintaining Readiness During Blocked Periods](./maintaining-readiness-during-blocked-periods.md) - Framework for what productive blocked-period work looks like
- [Strategic Focus During Autonomous Sessions](./strategic-focus-during-autonomous-sessions.md) - Avoiding productivity traps generally
- [Scope Discipline in Autonomous Work](./scope-discipline-in-autonomous-work.md) - Staying focused on what matters

## Origin

Extracted from agent behavior during an extended blocked period where 50+ "status check" commits were created with no actual value. The trap compounds when the session harness encourages producing output as proof-of-work even when nothing new has happened.
