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

gptme skills compose. A workflow skill can reference other skills by name, and
gptme will load all of them when the workflow is triggered. Each sub-skill stays
small and focused; the workflow skill is just a sequenced list of steps.

This gives you a library of reusable primitives that snap together into larger
automated workflows.

## Example

Three focused sub-skills:

```bash
# skills/release/check-tests.md
---
name: check-tests
description: Verify the test suite passes before any release action
keywords: [release, publish, ship, deploy]
---
# Check Tests Before Release

Always run the full test suite before any release action:
```bash
make test
```
If any test fails, STOP and fix it before proceeding with the release.
```

```bash
# skills/release/update-changelog.md
---
name: update-changelog
description: Changelog update convention for releases
keywords: [changelog, CHANGELOG, release notes]
---
# Changelog Update Convention

Format: `## [X.Y.Z] - YYYY-MM-DD` with sections Added / Changed / Fixed / Removed.
Always add the new version block at the TOP of CHANGELOG.md, above previous versions.
Never delete old entries.
```

```bash
# skills/release/tag-and-push.md
---
name: tag-and-push
description: Git tagging and push convention for releases
keywords: [tag, git tag, release tag, push tag]
---
# Tag and Push Convention

Create an annotated tag:
```bash
git tag -a v$VERSION -m "Release v$VERSION"
git push origin master --tags
```
Do not push without a passing CI run on master.
```

A workflow skill that composes all three:

```bash
# skills/release/workflow.md
---
name: release-workflow
description: Full release pipeline — test, changelog, tag, push
keywords: [do a release, release v, release workflow]
requires: [check-tests, update-changelog, tag-and-push]
---
# Release Workflow

Steps in order:
1. Run check-tests (stop on failure)
2. Update CHANGELOG.md with the new version
3. Commit the changelog: `git commit CHANGELOG.md -m "chore: update changelog for vX.Y.Z"`
4. Create the annotated tag and push per tag-and-push convention
```

Trigger the full workflow with one prompt:

```bash
gptme "Do a release for v1.4.2"
# gptme loads release-workflow, which loads the three sub-skills
# Agent follows the composed four-step pipeline
```

## Notes

- `requires:` lists sub-skills by name (matching their `name:` frontmatter). All
  required skills are injected into context before the workflow runs.
- Keep sub-skills under ~50 lines each. Long skills dilute focus; split them.
- Skills are versioned in git — use `git log skills/` to audit how your
  workflows have evolved over time.
