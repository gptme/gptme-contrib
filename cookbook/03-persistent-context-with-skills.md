---
title: Persistent Context with Skills
description: Use a project-local SKILL.md to make reusable context discoverable by name without re-explaining it every session
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

A skill is a plain markdown document (`SKILL.md`) that gptme discovers and lists
in the session's available-skills summary. Skills are placed in named
subdirectories under `skills/` (or `~/.config/gptme/skills/` for user-global
skills). The canonical format uses `name` and `description` frontmatter. gptme
auto-loads the full skill when its name appears in a message; the agent can also
read it on demand from the summary.

This means you write the explanation **once** and commit it. Future sessions can
load the same instructions instead of relying on repeated prompt setup.

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

Start a session and mention the skill by name when you want its full instructions:

```bash
gptme "Use db-migrations while adding a nullable email field to the User model"
# gptme auto-loads the db-migrations skill by name
```

You can also ask gptme to inspect the available-skills summary and load the most
relevant skill on demand.

## Notes

- Skills use `name` + `description` frontmatter (the canonical SKILL.md format).
  Give each skill a specific name; the description helps the agent choose among
  the skills listed in its prompt.
- Skill discovery is built into gptme's `LessonIndex`, which scans `./skills/`
  (and `~/.config/gptme/skills/` for global skills) at startup. No extra
  configuration is needed.
- If you need automatic topic-based matching, place a lesson under `lessons/`
  with `match.keywords` instead.
- The `SKILL.md` format is portable across Agent Skills-compatible runtimes,
  though loading behavior differs by runtime.
- Commit skills to your project repo so all contributors benefit from them.
