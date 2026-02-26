---
match:
  keywords:
    - commits behind
    - "⟳ behind"
    - "⚠ CONFLICTS"
    - mergeable=false
    - rebase based on behind count
    - rebasing without checking for conflicts
status: active
---

# When to Rebase PRs

## Rule
Only rebase PRs when there are merge conflicts, not just because they're behind master.

## Context
When reviewing PR status and considering whether to rebase them onto latest master.

## Detection
Observable signals indicating unnecessary rebase:
- Rebasing PRs solely because they show "X commits behind"
- Multiple rebase operations in quick succession
- Rebasing without checking for actual merge conflicts
- Wasting time on non-blocking issues

Common pattern: See "⟳ 5 behind" → immediately rebase → no actual conflict existed

## Pattern
Check for conflicts first, not just behind count:

```bash
# Wrong: rebase based on behind count
gh pr view <url>  # Shows "⟳ 5 behind"
cd /path/to/repo && git pull && git rebase origin/master
# Wastes time if no conflicts exist

# Correct: check mergeable status first
gh pr view <url>  # Check output
# - mergeable=true, state=clean → No rebase needed ✓
# - mergeable=false, state=dirty → Rebase to resolve conflicts
# - "⚠ CONFLICTS" indicator → Action needed

# Only rebase when conflicts exist
if [ "$mergeable" == "false" ]; then
    cd /path/to/repo && git rebase origin/master
fi
```

**Decision matrix**:
- Behind + No conflicts = No action needed (GitHub can merge)
- Behind + Has conflicts = Rebase to resolve conflicts
- Not behind = Never rebase

## Outcome
Following this pattern leads to:
- **Saved time**: No unnecessary rebases (common: 2-3 per day wasted)
- **Cleaner history**: Fewer spurious commits from rebases
- **Trust GitHub**: Let merge system handle non-conflicting changes
- **Focus on real issues**: Only rebase when conflicts block merge

Benefits demonstrated:
- GitHub successfully merges PRs that are behind master (no conflicts)
- Rebasing creates new commits that need review (unnecessary work)
- Checking mergeable status takes 5 seconds (vs 5 minutes for rebase)

## Related
- [Git Worktree Workflow](./git-worktree-workflow.md) - PR workflow in general
