---
match:
  keywords: ["create branch", "git checkout -b", "new feature branch", "PR contains unrelated commits"]
status: active
---

# Always Branch From Remote Master

## Rule
Create new branches from `origin/master`, never from local HEAD with uncommitted work.

## Context
When creating feature branches for PRs, branching from local commits that aren't on master causes PRs to contain unrelated commits.

## Detection
Observable signals:
- PR contains commits not mentioned in title/description
- PR requires rebase for clean review
- Reviewer questions why extra commits are present
- `git log origin/master..HEAD` shows local commits before branching

## Pattern

**Wrong - branches from current HEAD:**
```bash
# DON'T: Branches from wherever you are now
git checkout -b fix/my-feature
```

**Correct - branches from remote master:**
```bash
# Fetch latest master
git fetch origin master

# Create branch from origin/master
git checkout -b fix/my-feature origin/master

# Verify clean base
git log origin/master..HEAD  # Should show 0 commits initially
```

**For project monitoring:**
```bash
# Before creating branch for PR work
cd projects/gptme
git fetch origin master
git checkout master
git pull origin master

# Now create feature branch
git checkout -b fix/issue-123
```

## Outcome
Following this pattern:
- PRs contain only relevant commits
- Clean git history
- Easy review process
- No rebasing required
- Matches PR description exactly

## Related
