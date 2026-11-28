---
description: "In a checkout other sessions also edit, never stash, autostash, reset, clean or stage broadly: commit your own explicit paths, pull with --ff-only, and experiment in a separate worktree"
match:
  keywords:
    - "git stash"
    - "git pull --rebase --autostash"
    - "git reset --hard"
    - "git add -A"
    - "git commit -a"
    - "uncommitted changes disappeared"
    - "stash pop conflict"
    - "shared checkout"
  session_categories: [code, cross-repo]
target_grade: [harm]
status: active
---

# Shared Checkout Git Safety

## Rule
When several sessions (agents, humans, hosts) work in one checkout, treat every uncommitted change you did not make as someone else's work in progress. Never run a command that stashes, reverts, unstages or sweeps up the whole tree. Stage and commit your own explicit paths, pull with `--ff-only`, and do anything that needs a clean tree in a separate worktree.

## Context
Work is lost quietly, not loudly. `git stash` / `pull --autostash` move other sessions' edits into a stash entry; if the pop conflicts or never happens, the edits sit in an orphaned stash nobody looks at. A plain `prek install` / `pre-commit install` hook stashes unstaged changes on every commit and restores them afterwards; a concurrent edit makes that restore conflict. All worktrees of a repo share one `refs/stash`, so "stash, check, pop" can pop a sibling's entry. Global `pull.rebase=true` plus `rebase.autoStash=true` makes every plain `git pull` an autostash.

## Detection
- `git stash list` shows `autostash` or `On <branch>: ...` entries nobody remembers making
- A file you edited minutes ago is back to its committed content
- `git log -g refs/stash` shows stashes created by pre-commit or a rebase
- You are about to type `git stash`, `git add -A`, `git commit -a`, `git reset --hard`, `git checkout -- .`, `git restore .` or `git clean -fd` in a checkout you did not create for this task

## Pattern
```bash
# ❌ Sweeps up or sets aside other sessions' work
git stash && git pull --rebase && git stash pop
git add -A && git commit -m "fix"
git reset --hard origin/master

# ✅ Scoped to your own paths
git add path/to/my_file.py docs/my_doc.md
git commit -m "fix: thing" -- path/to/my_file.py docs/my_doc.md   # or git-safe-commit <paths>
git pull --ff-only                                                 # or git-safe-pull

# ✅ Need a clean tree or a baseline? Use a worktree or git show
git worktree add ../scratch origin/master
git show origin/master:path/to/file.py
```

## Setup
Run `scripts/git/setup-shared-checkout.sh [REPO]` once per shared checkout (`--check` to verify). It sets `safety.sharedCheckout`, `pull.ff=only` and local `rebase/merge.autoStash=false`, and installs a pre-commit hook that serializes commits and runs hooks on staged files only, so it never stashes. For Claude Code, add `scripts/claude-code-hooks/shared-checkout-git-guard.py` as a PreToolUse Bash hook: it denies the commands above in marked checkouts and stash-stack writes in any repo with linked worktrees.

## Recovery
If work went missing, look before acting: `git stash list`, `git fsck --unreachable | grep commit` (dangling stash commits), and `git show <stash>^3` for the untracked part of a `stash -u`. Apply with `git stash apply <sha>`, never `pop`, and only after checking the entry is yours.

## Outcome
- Other sessions' edits stay in the tree until they commit them
- Commits contain only what you meant to commit
- No orphaned stashes holding lost work

## Related
- [Git Worktree Workflow](./git-worktree-workflow.md)
- [Git Workflow](./git-workflow.md)
- `scripts/git/README.md` (git-safe-commit, git-safe-pull, git-safe-push-master)
