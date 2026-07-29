---
title: Compose Skills into Workflows
description: Chain multiple skills together to build repeatable, multi-step workflows from composable pieces
category: skill-composition
difficulty: advanced
tags: [skills, composition, workflows, automation]
deep_link: "https://gptme.ai/?prompt=I+want+to+build+a+release-workflow+skill+that+chains+test%2C+changelog-update%2C+and+tag+skills+together."
---

# Compose Skills into Workflows

## Problem

Real-world workflows are multi-step: you run tests, update a changelog, bump a
version, create a tag, post a release. Writing a single monolithic prompt for
this is brittle — it's hard to update one step without breaking others, and you
can't reuse the individual steps elsewhere.

## Solution

gptme skills can compose by reference. Keep each reusable step in a focused
skill, then create a workflow skill that names those component skills and tells
the agent to load them before executing the sequence. The available-skills
summary lets the agent find every named component without stuffing all of their
contents into every session.

This gives you reusable primitives that snap together into larger workflows
while keeping loading explicit and predictable.

## Example

Three focused sub-skills, each in its own `SKILL.md`:

```bash
# skills/release-check-tests/SKILL.md
---
name: release-check-tests
description: Verify the test suite passes before any release action
---
# Check Tests Before Release

Always run the full test suite before any release action:
run: make test
If any test fails, STOP and fix it before proceeding with the release.
```

```bash
# skills/release-update-changelog/SKILL.md
---
name: release-update-changelog
description: Changelog update convention for releases
---
# Changelog Update Convention

Format: `## [X.Y.Z] - YYYY-MM-DD` with sections Added / Changed / Fixed / Removed.
Always add the new version block at the TOP of CHANGELOG.md, above previous versions.
Never delete old entries.
```

```bash
# skills/release-tag-and-push/SKILL.md
---
name: release-tag-and-push
description: Git tagging and push convention for releases
---
# Tag and Push Convention

Create an annotated tag:
run: git tag -a v$VERSION -m "Release v$VERSION" && git push origin master --tags
Do not push without a passing CI run on master.
```

A workflow skill that explicitly composes all three:

```bash
# skills/release-workflow/SKILL.md
---
name: release-workflow
description: Full release pipeline that composes release-check-tests, release-update-changelog, and release-tag-and-push
---
# Release Workflow

Before starting, load these skills from the available-skills summary:

- `release-check-tests`
- `release-update-changelog`
- `release-tag-and-push`

Then follow their instructions in that order. Stop if the test step fails.
```

Trigger the full workflow with one prompt:

```bash
gptme "Use release-workflow to release v1.4.2"
# gptme loads release-workflow by name; its instructions direct the agent to
# read the three named component skills before executing the sequence
```

## Notes

- Composition is instruction-driven: gptme does not automatically resolve or
  inject skill dependencies. Name every component in the workflow skill.
- Each skill is a separate directory containing a single `SKILL.md` file.
- Keep sub-skills under ~50 lines each. Long skills dilute focus; split them.
- Skills are versioned in git — use `git log skills/` to audit how your
  workflows have evolved over time.
