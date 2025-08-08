# Git Workflow Defaults (Shared Lesson)

## Context
When making changes via gptme (agents and non-agent sessions), especially for docs/journals and small edits.

## Problem
Unnecessary branches/PRs for trivial changes, staging too much (git add .), leaking secrets, and submodule noise increase friction and review overhead.

## Constraint (Defaults)
- Small docs/journal tweaks: commit directly on master (no PR).
- Non-trivial/behavioral/code changes: ask first, then branch + PR if approved.
- Stage only the intended files; never use `git add .` or `git commit -a`.
- Use Conventional Commits; prefer a single, clear commit per small change.
- Don’t commit secrets or local tokens (e.g., gptme.toml env). Restore if accidentally edited.
- Treat submodules carefully: commit inside the submodule first, then update the pointer in the superproject.
- Don’t push or create PRs unless requested.

## Examples

Incorrect:
- Create a branch/PR for a single journal line.
- `git add .` and commit unrelated files.
- Commit gptme.toml containing tokens.

Correct:
- Commit only the journal:
  git add path/to/journal.md
  git commit -m 'docs: journal — add notes'
- Submodule update:
  (in submodule) git add <files> && git commit -m 'docs: ...'
  (superproject) git add submodule && git commit -m 'chore: bump submodule'

## Origin
Established 2025-08-08 from Erik/Bob session to reduce review overhead and PR noise.
