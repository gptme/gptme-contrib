# Autoresearch Program: gptme Eval Performance

## Objective

Improve gptme's pass rate on the `practical5` eval suite by making small, targeted
code changes to the gptme codebase. Each iteration makes ONE focused change, then
the harness evaluates and keeps or discards the change.

## Artifact

The gptme codebase in the workspace provided by the harness. You are working on
the current autoresearch branch in that workspace.
**Never modify tests. Never refactor.** Only fix bugs, improve tool behavior, or
fix parsing issues visible in the eval summary.

## ⚠️ CRITICAL: You are modifying gptme's SOURCE CODE

**DO NOT** write `pipeline.py`, `scrub.py`, or any other task solution files.
The eval runs gptme to solve those tasks — your job is to fix gptme so IT can
solve them correctly.

- You ARE modifying: `gptme/codeblock.py`, `gptme/tools/`, `gptme/llm/`, etc.
- You are NOT doing: writing the pipeline script, implementing regex scrubbing, etc.

If you find yourself writing Python that implements employee data filtering or
regex redaction, **STOP** — that is the eval task, not your job. Find the gptme
bug that prevents gptme from writing that code itself.

## Metric

Pass rate on the `practical5` eval suite (higher = better). The harness measures
this automatically after your change. Your job is to propose the change; the
harness decides whether to keep it.

## What practical5 tests

The practical5 suite tests three concrete tasks × 3 tool formats (markdown, tool, xml) = 9 total:
- `rename-function`: rename `calc_total` to `calculate_total` across multiple files
- `data-pipeline`: build `pipeline.py` that filters/transforms/aggregates employee data
- `regex-scrub`: build `scrub.py` that redacts emails, phone numbers, and SSNs

**Current baseline** (as of 2026-03-17, post-#1691 merge): 0.759 — CONFIRMED (session 163326)

NOTE: Baseline 0.759 was measured with claude-sonnet-4-6 as the eval model (Gemini at OR quota).
Gemini eval scores may differ. The 0.333 score in session 101523 used Gemini eval model.

**Target**: 0.889+ (8/9 passing)

**Known failing tests**: At least 2-3 tests still fail. Most likely xml and/or tool format
for data-pipeline and regex-scrub. Investigate with a fresh eval run to confirm.

**Fix in progress** (2026-03-17): PR #1692 fixes Gemini's ```tool_code block format for XML
mode. Gemini outputs `<save args="pipeline.py">code</save>` inside tool_code blocks, which
was previously ignored. After #1692 merges, expect XML format tests to pass for Gemini evals.

## ROOT CAUSE ANALYSIS (updated 2026-03-17)

### ✅ Issue 1: Markdown format — FIXED (gptme PR #1691, merged 2026-03-17)

The `</thinking>` handling fix is already in `gptme/codeblock.py` (commit 9ee3c6829).
**DO NOT re-implement this fix** — it's already present in the codebase.

Verify: `grep -n "thinking" gptme/codeblock.py` shows `["</thinking>", "</think>"]`.

Previous autoresearch iterations tried implementing this fix but scored lower because
they implemented it incorrectly. The correct implementation is already merged.

### Issue 2: XML format — FIXED in PR #1692 (2026-03-17, awaiting merge)

Gemini in XML mode wraps tool calls in ```tool_code blocks:
  ```tool_code
  <save args="pipeline.py">code</save>
  ```
Previously `_iter_from_xml` only recognized `<tool-use>` or `<function_calls>` wrappers,
so Gemini's format was silently ignored. PR #1692 adds a third format handler.
After #1692 merges, xml format tests for Gemini should pass for data-pipeline and regex-scrub.

### Issue 3: Tool format (tertiary)

Model outputs only "Thinking..." text with no JSON tool calls. Cause unknown.

## Anti-patterns (known to HURT performance)

**DO NOT modify these files — previous iterations show changes here consistently reduce score:**
- `gptme/eval/agents/__init__.py` (give-up instructions) — iter 1 on 2026-03-17 session: 0.333→0.222
- Any eval test definition files (`gptme/eval/suites/`)
- Any eval harness infrastructure files

These changes tend to break already-passing rename-function cases.

## How to find what's failing

**Start here**: Make the `gptme/codeblock.py` fix described above first.
If it doesn't improve the score, then look at eval output carefully:
1. Check workspace: does `pipeline.py` / `scrub.py` exist after gen?
2. If file missing → file creation failed → look at codeblock parsing
3. If file exists → logic error → look at the script content

## Decision process

1. Read the failing eval output carefully
2. Find the root cause — one specific bug or behavior issue
3. Make the minimal fix (usually 1-20 lines changed)
4. Stage with `git add <specific files>` — DO NOT commit
5. The harness will run the eval and decide to keep or discard

## Anti-patterns (don't do these)

- **Writing pipeline.py or scrub.py** — that's the eval task, not your job
- Broad refactoring (changes >50 lines)
- Modifying eval test definitions
- Changing the metric itself
- Making changes unrelated to the failing tests
- Committing (the harness does this)

## Format

After making your change, output a brief summary:
```
CHANGE: <one-line description of what you changed>
FILE: <path/to/changed/file.py>
REASON: <why this should improve the failing test>
```
