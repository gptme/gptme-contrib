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

gptme skills compose. Keyword coverage across multiple skills means that when a
high-level workflow keyword fires, each relevant sub-skill is injected into
context simultaneously. Each sub-skill stays small and focused; the workflow
skill is just a sequenced list of steps that references those sub-skills.

This gives you a library of reusable primitives that snap together into larger
automated workflows.

## Example

Three focused sub-skills:

Three focused sub-skills, each in its own `SKILL.md`:

```bash
# skills/release-check-tests/SKILL.md
---
match:
  keywords: [release, publish, ship, deploy]
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
match:
  keywords: [changelog, CHANGELOG, release notes]
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
match:
  keywords: [git tag, release tag, push tag]
description: Git tagging and push convention for releases
---
# Tag and Push Convention

Create an annotated tag:
run: git tag -a v$VERSION -m "Release v$VERSION" && git push origin master --tags
Do not push without a passing CI run on master.
```

A workflow skill that brings all three into scope by sharing keywords:

```bash
# skills/release-workflow/SKILL.md
---
match:
  keywords: [do a release, release v, release workflow, release, publish, ship, changelog, CHANGELOG, git tag, release tag]
description: Full release pipeline — test, changelog, tag, push
---
# Release Workflow

Steps in order:
1. Run `make test` (stop on failure)
2. Update CHANGELOG.md with the new version
3. Commit the changelog: `git commit CHANGELOG.md -m "chore: update changelog for vX.Y.Z"`
4. Create the annotated tag and push: `git tag -a v$VERSION -m "Release v$VERSION" && git push origin master --tags`
```

Trigger the full workflow with one prompt:

```bash
gptme "Do a release for v1.4.2"
# gptme matches keywords across all four skills and injects them all
# Agent follows the composed four-step pipeline
```

## Notes

- Composition works through keyword overlap: broad workflow keywords cover all
  sub-skill keywords so the full set is injected together.
- Each skill is a separate directory containing a single `SKILL.md` file.
- Keep sub-skills under ~50 lines each. Long skills dilute focus; split them.
- Skills are versioned in git — use `git log skills/` to audit how your
  workflows have evolved over time.
