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
session context automatically when it is relevant to the current conversation.
Skills are placed in named subdirectories under `skills/` (or `~/.config/gptme/skills/`
for user-global skills). The canonical format uses `name` and `description`
frontmatter; gptme matches skills via semantic similarity against the description.

This means you write the explanation **once**, commit it, and gptme uses it
whenever relevant — no copy-pasting, no per-session setup.

## Example

Create a skill for your project's database migration convention:

```bash
mkdir -p skills/db
cat > skills/db/SKILL.md << 'EOF'
---
name: db-migrations
description: Django migration conventions and pre-migration checklist. Applies when working with makemigrations, alembic, schema changes, or Django ORM models.
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
the conversation is about migrations, schema changes, or Django ORM work:

```bash
gptme "I need to add a nullable email field to the User model and run a migration"
# gptme injects the db-migrations skill automatically based on semantic similarity
```

## Notes

- Skills use `name` + `description` frontmatter (the canonical SKILL.md format).
  The `description` is what gptme matches against — write it to describe the
  scenarios where the skill should be injected.
- Skill discovery is built into gptme's `LessonIndex`, which scans `./skills/`
  (and `~/.config/gptme/skills/` for global skills) at startup. No extra
  configuration is needed.
- If you need literal keyword matching rather than semantic matching, place lesson
  files under `lessons/` with a `match.keywords` list instead.
- Skills work across runtimes (gptme terminal, gptme-contrib agents, Claude
  Code with the hook adapter). Write them once, use them everywhere.
- Commit skills to your project repo so all contributors benefit from them.
