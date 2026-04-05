# Autonomous Session Structure — Companion Doc

Full implementation details for the primary lesson at `lessons/autonomous/autonomous-session-structure.md`.

## Phase 1: Quick Status Check (2-3 minutes)

```bash
# Check for uncommitted changes (critical first step)
git status

# If uncommitted changes exist, commit them first
git add specific-files && git commit -m "descriptive message"

# Check recent context
ls -la journal/ | tail -5
head -20 journal/most-recent-entry.md
```

**Memory failure prevention**: Check for incomplete communication loops from previous sessions:
```bash
gh issue list --author @me --limit 5 --json number,title,url
git log --oneline --since="2 days ago" | grep -i "issue\|create\|comment" | head -3
```

If incomplete loops found → fix them immediately before starting new work.

## Phase 2: Task Selection (3-5 minutes)

### CASCADE Selection Order

**PRIMARY**: Check work queue
```bash
cat state/queue-manual.md | head -50  # "Planned Next" section
```

**SECONDARY**: Check notifications
```bash
gh api notifications --jq '.[] | {reason, subject: .subject.title}'
```

**TERTIARY**: Check workspace tasks
```bash
gptodo ready --json | jq '.ready_tasks[:3]'
gptodo status --compact
```

### Work Queue Best Practices

The work queue (`state/queue-manual.md`) tracks priorities across sessions:
- **Create** when you have clear priorities to track
- **Update** after each session (add new priorities, mark progress)
- **Evict** completed items and outdated state
- **Enrich** with links to issues, PRs, and related docs
- **Commit** queue changes for audit trail

Format:
```markdown
# Work Queue
**Last Updated**: YYYY-MM-DD HH:MM UTC

## Planned Next
### Priority 1: [Task Name]
**Tracking**: [Link to issue/PR]
**Next Action**: [Specific next step]
**Context**: [Key links, dependencies]
```

### Waiting vs Blocked

| State | Definition | Action |
|-------|-----------|--------|
| **Blocked** | Hard dependency not met | Cannot proceed |
| **Waiting** | Awaiting response | Move to next work |

```bash
# When task is waiting (not blocked):
gptodo edit <task> --set waiting_for "Response on PR #123"
gptodo edit <task> --set waiting_since 2025-01-10
# Then proceed to next ready work
```

Opening an issue ≠ blocked. It's an async handoff:
1. Open issue with clear question
2. Set `waiting_for` in task metadata
3. Move to SECONDARY/TERTIARY work immediately

## Phase 3: Work Execution (15-25 minutes)

- Make concrete progress on selected task
- Create journal entries documenting work
- Update task files with progress and status changes
- Focus on completable units of work

## Phase 4: Commit and Complete (2-3 minutes)

```bash
# Communication loop closure check
echo "Did I complete any requested actions needing follow-up?"
git log --oneline -5

# Stage only intended changes (never git add .)
git add specific-changed-files
git commit -m "type(scope): description"
git push origin master
```

## Time Allocation

| Phase | Percentage | Duration |
|-------|-----------|----------|
| Setup/Status | 15-20% | 5-7 min |
| Core Work | 70-80% | 20-25 min |
| Completion | 10-15% | 3-5 min |

## Work Selection Criteria

Good candidates:
- Can make meaningful progress in available time
- Have clear next steps or completion criteria
- Create value even if session ends mid-task
- Build on previous work rather than starting fresh

## Anti-Patterns

- Jumping to work without checking `git status`
- Session ending without committing and pushing
- Treating "waiting" as "blocked"
- Using `git add .` instead of specific files
- Missing communication loop closure at session end

## Example Session Flow

1. **Phase 1** (3 min): `git status` → clean. Check journal → understand context.
2. **Phase 2** (4 min): GitHub issue from collaborator (SECONDARY) takes priority over workspace tasks (TERTIARY).
3. **Phase 3** (22 min): Updated 2 task statuses, created architecture upgrade task, wrote 2 new lessons.
4. **Phase 4** (4 min): Stage specific files, commit with descriptive message, push.

**Result**: Task system cleaned up, priority work identified, concrete progress, 2 new lessons.
