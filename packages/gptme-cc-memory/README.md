# gptme-cc-memory

Typed, git-tracked, hook-injected cross-session memory for **Claude Code**:
plain Markdown memory files in your repo, scored against each prompt and
injected as context by a `UserPromptSubmit` hook — no vector DB, no LLM calls.

**Status:** experimental (`0.1.0`, alpha). Targets Claude Code hooks. For
gptme itself, see the [gptme-user-memories](../../plugins/gptme-user-memories/)
plugin and gptme's built-in lessons system.

## Why

Claude Code forgets everything between sessions, and most memory add-ons store
flat, untyped facts. This package adds four memory types whose type affects
retrieval scoring, and keeps every memory as a git-tracked Markdown file so
you get history and diffs for free (`git log memory/`).

| Type | What it encodes | Retrieval effect |
|------|-----------------|------------------|
| `feedback` | Behavioral rules from corrections or confirmations | Highest default confidence (0.88) and a 1.10 score boost |
| `user` | Who the user is — role, expertise, preferences | Default confidence 0.75 |
| `project` | Ongoing work, goals, decisions, deadlines | Default confidence 0.78 |
| `reference` | Where to find things in external systems | Default confidence 0.72, 0.96 score boost |

Properties:

- **Git-tracked** — memories are ordinary files in `<workspace>/memory/`.
- **Typed schema** — `name`, `description`, and `metadata.type` frontmatter,
  validated by `validate_memory_file()`.
- **Behavioral correction** — `feedback` memories carry **Why** and **How to
  apply**, giving the model enough context for edge cases.
- **Zero API cost** — pure file reads plus a small JSON state file.

## Install

Not published to PyPI. Install the console scripts from the repository
subdirectory:

```bash
uv tool install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-cc-memory"
# or, from a gptme-contrib checkout:
uv pip install -e packages/gptme-cc-memory
```

This provides three commands:

| Command | Purpose |
|---------|---------|
| `gptme-cc-memory-prompt-submit` | `UserPromptSubmit` hook: reads the hook JSON on stdin, prints the memory block to inject |
| `gptme-cc-memory-extract <trajectory.jsonl>` | Heuristic extractor over a Claude Code transcript |
| `gptme-cc-memory-stop-hook` | Thin wrapper that runs the extractor on `$CC_TRAJECTORY_FILE` (no-op if unset) |

## Quickstart

### 1. Create the memory directory

Memory lives at `<workspace>/memory/`, where `<workspace>` is
`$GPTME_CC_MEMORY_DIR` if set, otherwise the hook's working directory (your
project root under Claude Code).

```bash
mkdir -p memory
cp path/to/gptme-contrib/packages/gptme-cc-memory/MEMORY.md.template memory/MEMORY.md
```

`MEMORY.md` is a human/model-readable index; it is never itself injected.

### 2. Write a memory file

`memory/prefer-python-typing.md`:

```markdown
---
name: prefer-python-typing
description: Use Python typing hints for all function signatures
aliases: ["type hints"]   # optional extra match phrases
metadata:
  type: feedback
---

Always use Python type hints for function signatures.

**Why:** Prior review cycles were wasted adding type annotations that should
have been there from the start.

**How to apply:** Add return type annotations and argument type hints to every
new function. Use `| None` instead of `Optional[]`.
```

### 3. Register the prompt hook

In `.claude/settings.json` (or `.claude/settings.local.json`):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          { "type": "command", "command": "gptme-cc-memory-prompt-submit" }
        ]
      }
    ]
  }
}
```

On each prompt the hook scores every memory file and injects at most the two
most relevant, wrapped in `<memory_relevant_entries>`, capped at 4000
characters.

### 4. (Optional) Extract corrections at session end

`gptme-cc-memory-extract` reads a Claude Code transcript (JSONL) and, when
`GPTME_CC_MEMORY_DIR` is set, writes two handoff files into
`<workspace>/memory/`:

- `pending-updates.md` — detected corrections ("don't…", "always…"),
  confirmations, and guidance lines, appended to the file (corrections go
  under a dated `## Pending — YYYY-MM-DD HH:MM (corrections)` header). Review these and promote the real
  ones into typed memory files by hand.
- `pending-session-context.md` — the previous session's goal, last turn, and
  message/tool-call counts.

Claude Code passes the transcript path to hooks as `transcript_path` in the
stdin JSON, while `gptme-cc-memory-stop-hook` only reads `$CC_TRAJECTORY_FILE`.
Under plain Claude Code, a `Stop` hook that calls the extractor directly is the
simpler wiring:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "GPTME_CC_MEMORY_DIR=\"$CLAUDE_PROJECT_DIR\" gptme-cc-memory-extract \"$(jq -r .transcript_path)\""
          }
        ]
      }
    ]
  }
}
```

The extractor does **not** create memory files itself; it only produces the
pending handoff for review.

## How injection works

```text
UserPromptSubmit hook
  1. memory/guidance.md                -> injected once, then cleared
  2. memory/pending-updates.md         -> injected; dated blocks older than 3 days are skipped (file not rewritten)
  3. memory/pending-session-context.md -> injected once, then cleared
  4. memory/*.md (typed entries)       -> scored; top 2 above threshold injected
```

Scoring for typed entries:

```text
score = lexical_match × confidence × recency × type_boost × repeat_decay
```

- **lexical_match** — token overlap with `description`, body, and aliases
  (`name`, filename stem, and `aliases:`); a multi-word alias appearing in the
  prompt adds a strong bonus. At least two overlapping non-stopword tokens are
  required.
- **confidence** — per-type default, overridable per entry in the state file.
- **recency** — exponential decay with a 45-day half-life (floor 0.18), from
  `last_verified` in the state file or the file's mtime.
- **repeat_decay** — an entry injected within the last 45 minutes is
  suppressed (ramping from 0.15 back to 1.0), so the same fact is not
  re-injected on every prompt; a strong match can still break through.

Entries scoring below 0.85 are dropped. Retrieval state (`last_injected`,
`injections`, optional `confidence` / `last_verified`) lives in
`<workspace>/state/cc-memory/metadata.json` — add `state/` to `.gitignore` if
you don't want it tracked.

Files never treated as memory entries: `MEMORY.md`, `MEMORY-archive.md`,
`guidance.md`, `pending-updates.md`, `pending-session-context.md`, and the
retired `pending-items.md`.

## Python API

```python
from pathlib import Path
from gptme_cc_memory import discover_memory_files, parse_memory_file, validate_memory_file, load_yaml_frontmatter

for mem in discover_memory_files(Path("memory")):
    print(mem.type, mem.name, mem.description)

meta, body = load_yaml_frontmatter(Path("memory/prefer-python-typing.md").read_text())
print(validate_memory_file(meta, body))  # [] when valid
```

Scoring and injection helpers live in `gptme_cc_memory.memory_retrieval`
(`select_relevant_memories`, `render_relevant_memory_block`) and
`gptme_cc_memory.injector` (`inject_memories`).

## Package layout

```text
src/gptme_cc_memory/
  schema.py            # memory types, frontmatter parsing, validation, discovery
  memory_retrieval.py  # scoring, state file, rendering
  injector.py          # assembles the injected block (guidance, pending, entries)
  extractor.py         # heuristic transcript extractor (regex, no LLM)
  hooks/
    prompt_submit.py   # gptme-cc-memory-prompt-submit
    stop_hook.py       # gptme-cc-memory-stop-hook
    stop_hook.sh       # shell variant of the stop hook
MEMORY.md.template     # empty index template
tests/                 # schema, retrieval, injector, extractor tests
```
