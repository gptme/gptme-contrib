---
title: Persistent Context with Skills
description: Use SKILL.md files to inject project-specific context automatically without re-explaining it every session
category: context-management
difficulty: intermediate
tags: [skills, context, project-setup, efficiency]
deep_link: "https://gptme.ai/?prompt=Create+a+SKILL.md+for+my+Django+project+that+explains+our+model+naming+convention+and+migration+workflow."
---

# Persistent Context with Skills

## Problem

Every new gptme session starts blank. If your project has conventions (a
specific naming scheme, a non-obvious architecture decision, a required workflow
before running tests), you end up pasting the same explanation repeatedly or
getting wrong first drafts because the agent lacked context.

## Solution

A skill is a plain markdown document (`SKILL.md`) that gptme injects into the
session context automatically when certain keywords appear in the conversation.
Skills are placed in named subdirectories under `skills/` (or `~/.config/gptme/skills/`
for user-global skills) and matched by the `match.keywords` frontmatter field.

This means you write the explanation **once**, commit it, and gptme uses it
whenever relevant — no copy-pasting, no per-session setup.

## Example

Create a skill for your project's database migration convention:

```bash
mkdir -p skills/db
cat > skills/db/SKILL.md << 'EOF'
---
match:
  keywords: [migration, alembic, makemigrations, schema, django]
description: Django migration conventions and pre-migration checklist
---

# Migration Workflow

## Before creating a migration

1. Run `make lint` — schema changes that fail lint cause corrupt migrations.
2. Squash pending migrations if there are more than 10: `python manage.py squashmigrations`.
3. Check `git log --oneline migrations/` — never create a migration on a feature branch
   that is not yet merged; it will conflict with main's migration history.

## Creating a migration

```bash
python manage.py makemigrations --name describe_the_change
```

## Reviewing a migration

- Ensure `dependencies` lists only the single latest migration in each app.
- Confirm no `RunPython` migration operates on more than 1M rows without a batch loop.

## Reverting a migration

```bash
python manage.py migrate myapp 0042  # the migration before yours
```
EOF
```

gptme automatically discovers skills in the `skills/` directory at your project
root (no configuration needed). Alternatively, place skills in
`~/.config/gptme/skills/` for user-global availability across all projects.

Start a session and gptme will automatically inject the migration skill when
you mention "migration", "schema", or "makemigrations":

```bash
gptme "I need to add a nullable email field to the User model and run a migration"
# gptme injects migration-workflow.md automatically
```

## Notes

- Skills are injected by keyword match — keep keyword lists specific enough to
  avoid false triggers on unrelated topics.
- Skill discovery is built into gptme's lesson index (`LessonIndex`), which
  recursively scans `./skills/` (and `~/.config/gptme/skills/` for global skills)
  for `*.md` files at startup. No extra configuration is needed.
- Skills work across runtimes (gptme terminal, gptme-contrib agents, Claude
  Code with the hook adapter). Write them once, use them everywhere.
- Commit skills to your project repo so all contributors benefit from them.
