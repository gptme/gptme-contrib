# gptme-cc-memory

Typed, git-tracked, hook-injected session memory for Claude Code.

## Why

Claude Code forgets everything between sessions. At least 8 memory tools exist —
but all share the same architectural gap: **flat, untyped, untracked facts**.

`gptme-cc-memory` changes that with four memory types that encode *what-to-do-with-this*
semantics:

| Type | What it encodes | When it's injected |
|------|----------------|-------------------|
| `user` | Who the user is — role, expertise, preferences | When tailoring depth or framing |
| `feedback` | Behavioral rules from corrections or confirmations | **Always** (behavioral overrides) |
| `project` | Ongoing work, goals, decisions, deadlines | When working on related tasks |
| `reference` | Where to find things in external systems | When that system is mentioned |

### Key differentiators

- **Git-tracked** — every memory has full history (`git log memory/`, `git diff`)
- **Typed schema** — injection priority depends on memory type, not just cosine similarity
- **Behavioral correction** — `feedback` type includes **Why** and **How to apply**, giving
  the model enough context to handle edge cases
- **Bidirectional pipeline** — `stop-hook` extracts corrections from session trajectories;
  `prompt-inject` surfaces relevant memories at the next session start
- **Zero API cost** — pure file reads, no LLM calls for retrieval

## Installation

```bash
# From gptme-contrib
uv pip install -e packages/gptme-cc-memory

# For tests
uv pip install -e "packages/gptme-cc-memory[test]"
```

## Quick Start

### 1. Initialize memory directory

```bash
mkdir -p ~/.claude/projects/my-project/memory/
cp packages/gptme-cc-memory/MEMORY.md.template my-project/memory/MEMORY.md
```

### 2. Create your first memory file

```markdown
---
name: prefer-python-typing
description: Use Python typing hints for all function signatures
metadata:
  type: feedback
---

Always use Python type hints for function signatures.

**Why:** Prior review cycles were wasted adding type annotations that should
have been there from the start.

**How to apply:** Add return type annotations and argument type hints to every
new function. Use `| None` instead of `Optional[]`.
```

### 3. Add the stop hook

Add to your `.claude/settings.local.json`:

```json
{
  "hooks": {
    "PostToolUseSubmit": "gptme-cc-memory-stop-hook"
  }
}
```

### 4. The pipeline runs automatically

- **Stop hook**: After each session, the extractor reads the trajectory and writes
  pending updates, pending items, and new feedback memories.
- **Prompt injection**: At the next session start, `prompt-inject` reads the memory
  directory, scores each file, and injects the top-N relevant memories.

## Architecture

```
Interactive session ends
        │
        ▼
   stop-hook (async)
        │
        ▼
   extractor
     • Reads CC trajectory (JSONL)
     • Detects: corrections, confirmations, new instructions
     • Writes pending-updates.md + pending-items.md
        │
        ▼ (next session starts)
        │
   UserPromptSubmit hook fires
        │
        ▼
   injector
     • Reads memory/ directory
     • Scores each file: lexical match × confidence × recency decay
     • Selects top-N by type priority
     • Injects as additionalContext (stdout → CC harness)
```

## Memory File Format

Every memory file must have YAML frontmatter:

```markdown
---
name: short-kebab-case-slug
description: one-line hook for retrieval scoring
metadata:
  type: user | feedback | project | reference
---

[Memory body]

For feedback type, include:
**Why:** [reason the rule exists]
**How to apply:** [when/where this guidance kicks in]
```

## Package Structure

```
gptme-cc-memory/
  src/gptme_cc_memory/
    __init__.py         # Package exports
    schema.py           # Memory type definitions and frontmatter validator
    memory_retrieval.py # Shared retrieval helpers (scoring, discovery, state)
    injector.py         # Prompt-inject logic (scoring + injection)
    extractor.py        # Heuristic extractor (no LLM dependency)
    hooks/
      __init__.py       # Empty init
      stop_hook.sh      # Template stop hook
      prompt_submit.py  # UserPromptSubmit hook implementation
  MEMORY.md.template    # Empty memory index template
  README.md
  pyproject.toml
  tests/
    test_schema.py      # Schema validation tests
    test_retrieval.py   # Retrieval scoring tests
```

## Related

- [Design doc](https://github.com/ErikBjare/bob/blob/master/knowledge/technical-designs/typed-memory-schema-design.md)
- [Peer research](https://github.com/ErikBjare/bob/blob/master/knowledge/research/2026-06-22-cc-memory-ecosystem-recall-analysis.md)
