# gptme-lessons-extras

Tooling for maintaining a gptme **lessons** library: a lesson/skill format validator, plus
LLM-assisted lesson generation, usage analytics, similarity/duplicate detection and
effectiveness tracking from conversation logs.

**Status:** internal toolbox, experimental. `lesson-select` is the opt-in shared selector CLI;
`lesson-loo` measures observational outcome associations. Other modules run with `python3 -m` or by path. The validator is the most actively maintained part; the
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
uv pip install -e packages/bobutils -e packages/gptme-lessons-extras
```

Depends on `gptme` (used by the LLM-backed generation/judging helpers), `click`, `PyYAML`,
`python-frontmatter`, `rich`, and the repository-local `bobutils` frontmatter splitter.
Install `bobutils` alongside this package as above; workspace installs resolve it automatically.

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

Each worker attempt permits at most **one actual provider request**, including
SDK retries and provider fallback. `max_output_tokens` defaults to 512 and accepts
integers from 1 to 512. `max_input_bytes` defaults to 65,536 and accepts integers
from 1 to 65,536. The core enforces that byte cap on the rendered input and the
serialized provider input before sending. These bounds constrain requests; they
do not authorize spend or establish a dollar ceiling. Strict bounded calls require
an explicit `anthropic/` or `openrouter/` model supported by the core; unsupported
routes fall back without a provider call.
The worker also refuses enabled `GENERATION_PRE` or `GENERATION_POST` hooks with
`generation_hooks_present` before selection: independent work performed by hooks
is outside the core's request budget. Disabled hooks do not block selection.

`session_start` and `user_prompt` may make a call. `tool` can only read a valid
cached decision; a miss falls back without spending. Successful explicit none
is cacheable; failures are not. Fingerprints bind the entire eligible catalog
(IDs, descriptions, metadata and order), query, caller context fingerprint,
policy version, model, input/output caps and budget; event kind and remaining timeout do not
invalidate them. Cache/budget entries are namespaced by runtime and session.

Each call runs the existing gptme `LLMSelector` in an owned subprocess. The
remaining deadline (maximum 30 seconds) kills and reaps that subprocess on
timeout. Protocol inputs are limited to 1 MiB and 200 candidates; the stricter
provider-input cap above includes the rendered prompt overhead. Old gptme versions
whose parser cannot distinguish malformed output from explicit none, or whose
`ContextSelectorConfig` lacks any of `max_tokens`, `max_input_bytes`, and
`max_provider_requests`, fall back with `unsupported_core` **before selection or
contacting a provider**. Parser compatibility requires the fix in
[gptme/gptme#4282](https://github.com/gptme/gptme/pull/4282); parser compatibility
alone does not establish cost-bound support. Missing
dependencies, provider failures and invalid output also preserve fallback.
Provider diagnostics cannot contaminate protocol stdout.

Runtime adapters and a paired-runtime efficacy experiment are separate rollout
steps. Installing this package alone is not evidence of production adoption.

## Lesson outcome associations (`lesson-loo`)

`lesson-loo` compares session outcomes with and without each lesson. This is an
**observational association**, despite the historical LOO name: sessions are not
rerun with one lesson removed, and a delta does not establish benefit or harm.
Agent-specific bandits, grading, lifecycle decisions and record adapters stay
outside the shared core.

```bash
lesson-loo sessions.jsonl --min-sessions 5
python3 -m gptme_lessons_extras.loo sessions.jsonl --category-controlled
```

The input is one JSON object per session, with a stable unique `session_id`, a
finite numeric `outcome` in `[0, 1]` (higher is better), and a `lessons` array:

```json
{"session_id":"run-1","outcome":0.8,"category":"code","lessons":[{"id":"verify-before-claiming","trigger_accuracy":0.9,"keyword":"claiming fixed without testing","event":"user_prompt"}]}
```

Lesson IDs are opaque strings; the command never opens them as file paths.
`category`, `keyword`, `event` and `trigger_accuracy` are optional. An empty
`lessons` array means **confirmed no injections**; missing exposure evidence must
be excluded before export, rather than represented as an empty array. Repeated
lesson IDs within a session count once for outcome comparisons, while every
emission's metadata is retained. Relevance averages include all rated emissions,
and any `error_recovery` emission flags the session regardless of emission order.
Duplicate session IDs are rejected: join and deduplicate source events before
analysis.

Normalize outcomes to `[0, 1]` using one comparable scale and rubric across the
cohort (for example, divide a 0–5 grade by 5). Raw durations or unbounded numeric
measurements are not outcome scores; convert them using a fixed rubric first.
Booleans,
missing values and non-finite numbers are invalid. For example, encode an
independently assessed success as numeric `1` and failure as `0`.
`trigger_accuracy`, when available, is a separate relevance judgment in `[0, 1]`.
It is averaged across rated injections and remains null when unmeasured; do not
infer it from the outcome or treat a matcher score as a measured accuracy.

A minimal runnable example (too small for interpretation):

```bash
cat > sessions.jsonl <<'JSONL'
{"session_id":"a","outcome":0.8,"lessons":[{"id":"verify"}]}
{"session_id":"b","outcome":0.6,"lessons":[]}
JSONL
lesson-loo sessions.jsonl --min-sessions 1
```

The JSON report on stdout includes per-lesson counts, means, `delta` (with minus
without), sample-support `confidence`, trigger accuracy and warnings. Here the
raw delta is `0.2`; with one session per arm this says little. Confidence is
`min(1, min(n_with, n_without) / 30)`, **not a probability that the lesson works**.
Means and deltas use four decimal places, so tiny differences can round to zero.
Rows below `--min-sessions` per arm have null comparison statistics. The Welch
statistic uses an unadjusted normal-approximation p-value, which is unreliable for
small samples and does not correct for testing many lessons.

`--category-controlled` compares within each category with enough sessions in
both arms, then averages deltas weighted by category size. Categories lacking an
arm are excluded. Comparison counts and trigger averages use only eligible
categories; `exposed_sessions` and `unexposed_sessions` count the full corpus.
Means, `z_score` and `p_value_approx` are null in this mode: pooled statistics
can point in the opposite direction from the controlled delta and are not a
category-adjusted significance test. Category control cannot remove all selection
effects.

Warnings keep three recurring failure modes visible:

- **Error-recovery confounds:** lessons fired because work was already failing
  can correlate with worse outcomes. Set `event` to `error_recovery` when known;
  the report flags these exposures. Other trigger events can also be confounded.
- **Ghost lessons:** pass `--catalog catalog.json`, an object whose keys are the
  current lesson IDs (for example `{"verify": {}}`). Unlisted IDs are flagged;
  catalog values are ignored. A historical positive delta alone does not justify
  recreating a deleted or superseded lesson.
- **Over-grazing:** set `--analysis-passes` to the number of passes over this
  cohort. At two or more passes the report warns about repeated same-data tuning.
  It does not block read-only analysis. Preserve a holdout or collect fresh data
  before using a tuned ranking to justify changes.

### Producing rows from Claude Code sessions

Instrument the hook that emits lesson hints through `additionalContext` to record
**the IDs actually emitted**, keyed to the same stable session ID as the outcome.
Group those emissions per session, then join the independent outcome signal and
export the JSONL contract above. Repeated IDs may remain in the array to preserve
per-emission relevance ratings, matched keywords and hook-event metadata. Hook output records establish emission; they do not prove
that the model read or followed the hint. Transcript prose, candidate-selection
receipts and keyword-match logs alone do not establish actual lesson injection.

A transcript importer is intentionally not supplied: emission provenance and
transcript layouts vary by recorder. Verify the local recording contract instead
of guessing exposure from mentions. Use comparable outcomes such as a fixed human
rubric (task resolved without correction), meaningful validation results, or a
blinded session judge. Keep outcome collection independent of whether the lesson
was present, and include scored sessions both with and without it.

An interactive agent with no recorded outcomes **cannot compute a
meaningful outcome delta yet**. Record a consistent outcome signal first; injection
logs alone support exposure counts, not lesson effectiveness claims. This command
does not grade sessions, change lesson status, or update a bandit.

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

It also checks frontmatter keywords and that an optional `target_grade` is one of
`trajectory_grade`, `productivity`, `alignment`, `harm`. `status` values are not validated;
`status: archived` only relaxes the required-section checks.

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

Run inside an agent workspace checkout (most default to `./lessons` and gptme's log directory
`~/.local/share/gptme/logs`; `analytics` resolves `lessons/` from the nearest enclosing `.git`
root and reports "Lessons directory not found" outside a git repository). Use `--help` on any click-based module for full options.

| Module | What it does |
|--------|--------------|
| `python3 -m gptme_lessons_extras.generate` | LLM lesson pipeline: `workflow` (analyze → generate → judge a conversation), `generate`, `evolve` (multi-variant, Pareto selection), `judge`, `deduplicate`. Drafts go to `knowledge/meta/lessons-draft/` |
| `python3 -m gptme_lessons_extras.analytics` | Which lessons are referenced in gptme conversation logs; writes `knowledge/meta/lesson-usage-report.md` |
| `python3 -m gptme_lessons_extras.effectiveness_tracker` | Incremental, resumable correlation of lesson inclusion with session outcomes (`--report`, `--limit`, `--full`, `--reset`, `--logs-dir`, `--state-file`) |
| `python3 -m gptme_lessons_extras.discovery` | `recommend` lessons for context keywords, find `similar` lessons, list `duplicates` |
| `gptme_lessons_extras.similarity` / `utils.similarity` | Keyword/content similarity and recency scoring (library) |
| `analyze-lesson-usage.py`, `improve-lesson-keywords.py`, `detect-lesson-patterns.py`, `generate-review-prompts.py`, `create-pr.py` | Standalone maintenance scripts — run by path from `src/gptme_lessons_extras/` |
| `export`, `import`, `sync`, `review`, `adopt`, `evolution`, `metrics`, `network_schema` | Experimental sharing of lessons between agents via a git repo |

For staleness analysis, use `similarity.calculate_recency_scores(lessons_dir, usage_stats)`;
`usage_stats` is optional and maps relative lesson paths to `(last_referenced, reference_count)`.
The standalone `check-staleness.py` report script was removed; its fixed analytics/report
paths and threshold rules are no longer maintained.

### Effectiveness tracker state upgrades

Incremental tracking now records exact processed log names rather than a name
watermark. A legacy state with a watermark but no processed-name list is kept
unchanged and analysis asks for an explicit `--full` rebuild. Back up that state
and restore archived logs before rebuilding if their statistics must be retained:
`--full` replaces totals using only available logs. A missing logs directory
aborts a rebuild without overwriting state. A limited rebuild (`--full --limit N`)
or one interrupted after a checkpoint can be resumed with an ordinary incremental
run (omit `--full`); processed names prevent skipped or double-counted logs.
`last_processed` remains the last visited name, not a completeness watermark.

## Notes

- Import as `gptme_lessons_extras`. In source checkouts, a `src/lessons` symlink keeps the legacy
  `from lessons import ...` path working.
- Tests: `uv run pytest packages/gptme-lessons-extras/tests/`
