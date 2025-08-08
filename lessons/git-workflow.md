# Git Workflow Defaults (Shared Lesson)

## Context
When making changes via gptme (agents and non-agent sessions), especially for docs/journals and small edits.

## Problem
Unnecessary branches/PRs for trivial changes, staging too much (git add .), leaking secrets, and submodule noise increase friction and review overhead.

## Defaults (concise rules)
- Small docs/journal tweaks: commit directly on master (no PR).
- Non-trivial/behavioral/code changes: ask first; if approved, branch + PR.
- Stage only intended files; never use `git add .` or `git commit -a`.
- Use Conventional Commits; single, clear commit for small changes.
- Never commit secrets/tokens (e.g., gptme.toml env); restore if edited.
- Submodules: commit inside the submodule first, then update the superproject pointer.
- Don’t push or create PRs unless requested.

## Step-by-step workflow
1) Decide scope
   - If trivial docs/journal → commit on master.
   - If non-trivial/needs review → ask; then branch + PR if approved.
   - Avoid: creating branches/PRs for tiny edits.

2) Prepare working tree
   - Run `git status` to see exactly what changed.
   - If sensitive files (e.g., gptme.toml) were touched, `git restore <file>`.
   - Avoid: carrying unrelated/untracked files into commits.

3) Stage
   - Stage only explicit paths: `git add <path1> <path2>`.
   - Avoid: `git add .` and `git commit -a`.

4) Commit
   - Use Conventional Commits (e.g., `docs: …`, `fix: …`).
   - Keep commits focused; prefer one clear commit per small change.
   - Avoid: vague messages or bundling unrelated changes.

5) Submodules (when applicable)
   - In the submodule: commit the actual file changes.
   - In the superproject: `git add <submodule>` and commit (“chore: bump …”).
   - Avoid: editing submodule files only from the superproject without committing inside the submodule.

6) Push/PR
   - Don’t push or open PRs unless requested.
   - If on a feature branch and review is desired: push and open PR with a clear title/body.
   - Avoid: pushing master without confirmation for non-trivial changes.

## Origin
Established 2025-08-08 from Erik/Bob session to reduce review overhead and PR noise.
