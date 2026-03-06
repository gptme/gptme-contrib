---
match:
  keywords: ["gptme-contrib", "contribute lesson", "contrib PR", "external contribution", "submit lesson", "upstream lesson"]
status: active
---

# gptme-contrib Contribution Pattern

## Rule
Follow a structured five-phase workflow when contributing lessons or code to gptme-contrib to ensure high-quality submissions that pass automated review and reach the maintainer in merge-ready state.

## Context
When contributing lessons, documentation, or code to the gptme-contrib shared repository.

## Detection
Observable signals that you need this workflow:
- Writing a lesson you want to share with all gptme agents
- Preparing PRs for gptme-contrib
- Upstreaming a pattern from your personal workspace
- Addressing automated review feedback on a contrib PR

## Pattern

### Phase 1: Content Preparation

**Before writing:**
```bash
# Check peer files for frontmatter schema — MANDATORY first step
ls gptme-contrib/lessons/<category>/
head -15 gptme-contrib/lessons/<category>/existing-lesson.md

# Workflow files use: match.keywords (array) + status
# Pattern files use: match.keywords (block) + status
# Do NOT add category/tags fields unless they appear in peer files
```

**While writing:**
- Follow the standard structure: Rule → Context → Detection → Pattern → Anti-Patterns → Outcome → Related → Origin
- Detection section is required (see `lesson-quality-standards.md`)
- Verify all numeric claims (if text says "five patterns", count them — make sure it IS five)
- For code examples with sets, use `sorted()` for deterministic output in error messages:

```python
# Wrong: non-deterministic set ordering (Python hash randomization)
raise ValueError(f"Expected one of {VALID_OPTIONS}")

# Correct: stable output
raise ValueError(f"Expected one of {sorted(VALID_OPTIONS)}")
```

- For bash file-existence checks, use `[[ -f file ]]` not `ls file 2>/dev/null`

**Before committing, validate locally:**
```bash
cd gptme-contrib
pre-commit run --all-files
```

### Phase 2: Branch and PR Creation

```bash
# Create branch from origin/master — not from a local branch
git checkout -b feat/descriptive-branch-name origin/master

# Add and commit
git add lessons/category/lesson-name.md
git commit -m "docs(lessons): add <topic> lesson"

# Push and open PR
git push -u origin feat/descriptive-branch-name
gh pr create --title "docs(lessons): add <topic> lesson" --body "..."
```

**PR description should include:**
- What pattern the lesson covers and why it's valuable
- Where the pattern was observed (origin story)
- Links to related lessons if applicable

### Phase 3: Automated Review Response

Greptile reviews every PR. **Address all feedback within the same session** (or within hours). Common issues it flags:

| Issue | Fix |
|-------|-----|
| Frontmatter fields not in peer files | Remove non-standard fields |
| "Five patterns" but six defined | Recount and fix the text |
| `{VALID_OPTIONS}` in f-string | Change to `{sorted(VALID_OPTIONS)}` |
| `ls file 2>/dev/null` in bash | Change to `[[ -f file ]]` |
| Missing Detection section | Add it before the Pattern section |

**Reply template:**
```markdown
Thanks for the review!

Addressed in <commit-hash>:
- **<issue 1>** — <what you changed>
- **<issue 2>** — <what you changed>
```

### Phase 4: Maintainer Approval

After Greptile approves (or raises no blockers), wait for human maintainer review. No action
needed until feedback arrives. Don't ping or rebase while waiting.

### Phase 5: Post-Merge

- Check that the lesson appears at the expected path in the repo
- Update your personal workspace to point to the contrib version (or delete the duplicate)
- Note the lesson PR number in your origin section for future reference

## Anti-Patterns

**❌ Not checking peer frontmatter first:**
Adding `category: workflow` and `tags: [git]` fields to a file where every peer uses only
`match.keywords` + `status`. Greptile will flag this as a schema inconsistency.

**❌ Inaccurate count claims:**
Writing "All five patterns share..." when the lesson defines six sections (1–6). Count
the sections, then write the number.

**❌ Non-deterministic set ordering:**
```python
raise ValueError(f"Expected one of {VALID_UNITS}")  # output varies run to run
```

**❌ Creating branch from local instead of origin/master:**
Contaminating the PR with unrelated commits from other local work.
See also: `clean-pr-creation.md`

**❌ Slow review response:**
Greptile leaves feedback. Leaving it unaddressed for days signals low engagement and
slows the merge cycle. Address feedback in the same or next session.

## Outcome

Following this workflow results in:
- **First-cycle merges**: Most PRs pass review without back-and-forth
- **Clean history**: Each PR is a focused, single-topic addition
- **Maintainer trust**: Consistent quality builds a reliable contributor reputation
- **Ecosystem value**: Lessons benefit all gptme agents going forward

## Related

- [Lesson Quality Standards](../autonomous/lesson-quality-standards.md) — Required lesson structure
- [Clean PR Creation](./clean-pr-creation.md) — Keeping PRs free of unrelated commits
- [Pre-Landing Self-Review](./pre-landing-self-review.md) — Final review before submitting

## Origin
2026-03-06: Extracted from the contribution workflow across multiple gptme-contrib lesson PRs.
The Greptile review patterns (frontmatter drift, count mismatch, set ordering, bash anti-pattern)
appeared consistently across PRs #369–#371 and are the main failure modes to avoid.
