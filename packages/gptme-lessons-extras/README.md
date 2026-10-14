# gptme-lessons-extras

Tooling for maintaining a gptme **lessons** library: a lesson/skill format validator, plus
LLM-assisted lesson generation, usage analytics, similarity/duplicate detection and
effectiveness tracking from conversation logs.

**Status:** internal toolbox, experimental. `lesson-select` is the opt-in shared selector CLI;
other modules run with `python3 -m` or by path. The validator is the most actively maintained part; the
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

## Shared advisory selector (opt-in)

`lesson-select --context-json request.json --state-dir /path/to/selector-state`
(or `python3 -m gptme_lessons_extras.selector`) returns one JSON decision on
stdout. Omit `--context-json` or pass `-` to read stdin. The Python API is
`selector.select(request, state_dir=Path(...))` and returns the same dictionary.
This service does not enable or change any runtime hook by itself.

Example request (offline: no model or call budget is enabled):

```json
{
  "version": 1,
  "runtime": "gptme",
  "session_id": "example-session",
  "event_kind": "session_start",
  "context_fingerprint": "implement-config-parser",
  "policy_version": "advisory-v1",
  "query": "Implement a configuration parser with regression tests",
  "candidates": [
    {"id": "test-first", "description": "Reproduce parser failures before fixing", "metadata": {}}
  ],
  "fallback_ids": ["test-first"],
  "max_results": 3,
  "model": null,
  "max_calls_per_session": 0,
  "timeout_seconds": 2
}
```

Decisions are `selected` (ordered unique IDs), `empty` (explicit valid none), or
`fallback` (use the returned deterministic IDs). Results include `version`,
`ids`, `reason`, `cached`, `fingerprint`, `runtime`, `session_id`, `event_kind`,
`calls_used`, and `elapsed_ms`; this is also the shared receipt format. Invalid
request schemas return `fallback/invalid_request` with no IDs, so adapters must
retain their own baseline if they cannot construct/validate the protocol.
Unknown selected IDs and malformed worker output cause fallback, never a
successful empty decision. Fallback IDs are restricted to supplied candidates,
deduplicated, and capped.

Every valid request appends a best-effort `selection_receipt` with these fields
and a UTC timestamp to `state_dir/receipts.jsonl`. Queries and candidate text are
not logged. These are selection receipts, not proof of downstream injection or
use; join them with each runtime's emission/outcome records when evaluating.

Callers pass **only eligible advisory candidates**, after repository/category
filters and policy exemptions. Hard guards and memories stay outside this
service. Callers still own rendering, holdouts, and session injection dedup.
An ID is opaque: this service never loads a lesson path. Metadata can carry
`keywords`, `tools`, `priority`, and `tags` for the existing core prompt.

Paid selection requires an explicitly authorized `model` and a nonzero
`max_calls_per_session` (0 by default, maximum 10). This is an **attempt-count
ceiling, not a dollar limit**. Every started attempt, including failures and
timeouts, consumes one slot. SQLite reserves slots atomically across concurrent
requests and fingerprint changes. Keep `runtime`, `session_id`, and `state_dir`
stable for the session; deleting/replacing this state resets the counter.
The default directory is `$XDG_STATE_HOME/gptme/lesson-selector` (or
`~/.local/state/gptme/lesson-selector`). A state failure returns deterministic
fallback without a provider call. No API key or default model is added here.

`session_start` and `user_prompt` may make a call. `tool` can only read a valid
cached decision; a miss falls back without spending. Successful explicit none
is cacheable; failures are not. Fingerprints bind the entire eligible catalog
(IDs, descriptions, metadata and order), query, caller context fingerprint,
policy version, model, cap and budget; event kind and remaining timeout do not
invalidate them. Cache/budget entries are namespaced by runtime and session.

Each call runs the existing gptme `LLMSelector` in an owned subprocess. The
remaining deadline (maximum 30 seconds) kills and reaps that subprocess on
timeout. Inputs are limited to 1 MiB and 200 candidates. Old gptme versions whose
parser cannot distinguish malformed output from explicit none fall back with
`unsupported_core` **before contacting a provider**; this requires the parser
fix in [gptme/gptme#4282](https://github.com/gptme/gptme/pull/4282). Missing
dependencies, provider failures and invalid output also preserve fallback.
Provider diagnostics cannot contaminate protocol stdout.

Runtime adapters and a paired-runtime efficacy experiment are separate rollout
steps. Installing this package alone is not evidence of production adoption.

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
