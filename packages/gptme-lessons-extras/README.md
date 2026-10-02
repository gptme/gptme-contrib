# gptme-lessons-extras

Tooling for maintaining a gptme **lessons** library: a lesson/skill format validator, plus
LLM-assisted lesson generation, usage analytics, similarity/duplicate detection and
effectiveness tracking from conversation logs.

**Status:** internal toolbox, experimental. There are no installed console scripts — modules are
run with `python3 -m` or by path. The validator is the most actively maintained part; the
analytics, generation and agent-network sharing modules (`export`, `import`, `sync`, `review`,
`adopt`, `evolution`, `metrics`) are older and less polished.

## Why / when to use it

gptme injects lessons — short Markdown files with keyword-matching frontmatter — into an agent's
context when they are relevant ([gptme lessons docs](https://gptme.org/docs/lessons.html)). Core
gptme handles *matching*; this package helps you *curate* the library: keep the format consistent
in CI/pre-commit, find which lessons fire, spot duplicates, and draft new lessons from
conversations.

Related:

- [gptme-lessons-mcp](../gptme-lessons-mcp/README.md) — serve lessons to any MCP client
- [`lessons/`](../../lessons/) in this repo — shared lessons validated with this tool
- [gptme-agent-template](https://github.com/gptme/gptme-agent-template) — workspace layout with a
  `lessons/` directory

## Install

Not published on PyPI. From a clone of gptme-contrib:

```bash
uv pip install -e packages/gptme-lessons-extras
```

Depends on `gptme` (used by the LLM-backed generation/judging helpers), `click`, `PyYAML`,
`python-frontmatter` and `rich`.

## Quickstart: validate lessons

```bash
# Validate a directory (recursive by default) or individual files; exit code 1 on any failure
python3 -m gptme_lessons_extras.validate lessons/
python3 -m gptme_lessons_extras.validate lessons/workflow/my-lesson.md -v   # -v shows warnings
```

From Python:

```python
from pathlib import Path
from gptme_lessons_extras.validate import LessonValidator, validate_lesson_file

ok = validate_lesson_file(Path("lessons/workflow/my-lesson.md"))  # prints results, returns bool

v = LessonValidator(Path("lessons/workflow/my-lesson.md"))
v.validate()
print(v.format_type, v.errors, v.warnings)
```

As a pre-commit hook (requires the package to be importable by the hook's Python):

```yaml
- id: validate-lessons
  name: Validate lesson files
  entry: python3 -m gptme_lessons_extras.validate
  language: system
  files: ^lessons/.*\.md$
```

### What the validator checks

It auto-detects three formats:

- **Two-file (preferred):** a concise primary lesson (soft target ~100 lines) with sections
  `Rule`, `Context`, `Detection`, `Pattern`, `Outcome`, `Related`, plus an optional companion doc
  under `knowledge/lessons/`.
- **Original (verbose):** all-in-one lessons with `Rule`, `Context`, `Failure Signals`,
  `Anti-pattern (concise)`, `Recommended Pattern`, `Fix Recipe`, `Rationale`,
  `Verification Checklist`, `Exceptions`, `Automation Hooks`, `Origin`, `Related`.
- **Skill:** `SKILL.md` files, which must have `name` and `description` frontmatter.

It also checks frontmatter (keywords, `status`) and that an optional `target_grade` is one of
`trajectory_grade`, `productivity`, `alignment`, `harm`.

Minimal two-file lesson:

```markdown
---
match:
  keywords: ["specific trigger phrase", "another distinctive phrase"]
status: active
---

# Lesson Title

## Rule
One-sentence imperative: what to do or avoid.

## Context
When this applies.

## Detection
Observable signals.

## Pattern
Correct approach with example.

## Outcome
What happens when you follow this pattern.

## Related
- Related lessons
```

## Other tools

Run from the root of an agent workspace (most default to `./lessons` and gptme's log directory
`~/.local/share/gptme/logs`). Use `--help` on any click-based module for full options.

| Module | What it does |
|--------|--------------|
| `python3 -m gptme_lessons_extras.generate` | LLM lesson pipeline: `workflow` (analyze → generate → judge a conversation), `generate`, `evolve` (multi-variant, Pareto selection), `judge`, `deduplicate`. Drafts go to `knowledge/meta/lessons-draft/` |
| `python3 -m gptme_lessons_extras.analytics` | Which lessons are referenced in gptme conversation logs; writes `knowledge/meta/lesson-usage-report.md` |
| `python3 -m gptme_lessons_extras.effectiveness_tracker` | Incremental, resumable correlation of lesson inclusion with session outcomes (`--report`, `--limit`, `--full`, `--reset`, `--logs-dir`, `--state-file`) |
| `python3 -m gptme_lessons_extras.discovery` | `recommend` lessons for context keywords, find `similar` lessons, list `duplicates` |
| `gptme_lessons_extras.similarity` / `utils.similarity` | Keyword/content similarity and recency scoring (library) |
| `check-staleness.py`, `analyze-lesson-usage.py`, `improve-lesson-keywords.py`, `detect-lesson-patterns.py`, `generate-review-prompts.py`, `create-pr.py` | Standalone maintenance scripts — run by path from `src/gptme_lessons_extras/` |
| `export`, `import`, `sync`, `review`, `adopt`, `evolution`, `metrics`, `network_schema` | Experimental sharing of lessons between agents via a git repo |

## Notes

- Import as `gptme_lessons_extras`. In source checkouts, a `src/lessons` symlink keeps the legacy
  `from lessons import ...` path working.
- Tests: `uv run pytest packages/gptme-lessons-extras/tests/`
