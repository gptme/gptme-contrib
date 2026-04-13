---
match:
  keywords:
    - knowledge base
    - memory index
    - curated index
    - knowledge retrieval
    - MEMORY.md
    - auto-memory
status: active
---

# Indexed Knowledge Base Pattern for LLM Retrieval

## Rule
Structure knowledge as a directory of markdown files with a curated index file. The index is always loaded into context; detail files are loaded selectively by relevance. This gives LLMs efficient retrieval over a growing knowledge base without stuffing everything into the prompt.

## Context
LLMs need knowledge that grows over time — data inventories, design decisions, user preferences, domain facts. Dumping everything into a system prompt doesn't scale. But the LLM can't search files on its own without tooling.

The solution is a two-tier structure: a **compact index** (always in context) that points to **detail files** (loaded on demand). Claude Code's auto-memory system uses exactly this pattern, but it's general-purpose and works for any knowledge base.

## Pattern

### Structure
```
knowledge/
  INDEX.md              # Always loaded. ~50-200 lines max.
  topic-a.md            # Detail file, loaded when relevant
  topic-b.md            # Detail file, loaded when relevant
  ...
```

### The index file
Compact manifest with one-line descriptions per entry. Descriptions are the retrieval key — they're what the LLM (or tooling) matches against the current conversation to decide what to load.

```markdown
# Knowledge Index

## Data & Analysis
- [qs-data-landscape.md](qs-data-landscape.md) — QS data sources on erb-m2, how to query AW, Oura date ranges
- [predictive-framework.md](predictive-framework.md) — decay kernels, Bayesian sleep/work models, counterfactual simulation

## Feedback
- [feedback-journal-format.md](feedback-journal-format.md) — journal entries are topic-based, not session-based
```

### Detail files
Structured content with optional frontmatter for metadata:
```markdown
---
name: QS Data Landscape
description: What QS data is available on erb-m2 and how to access it
type: reference
---

Content goes here...
```

The `description` field is what retrieval matches against. Make it specific enough to distinguish from other entries — "QS data sources on erb-m2" not just "data stuff."

### Retrieval flow
1. Index is loaded into every conversation (cheap — it's small)
2. LLM or tooling scans descriptions against current context
3. Matching detail files are loaded into context
4. Non-matching files stay on disk, saving context window

### Version control
The whole directory lives in git. This is critical:
- Knowledge is versioned and reviewable
- Portable across machines (clone the repo)
- Visible to all runtimes (not locked to one tool)

### Symlink trick for runtime integration
If a runtime has its own local knowledge store with retrieval built in (like Claude Code's `~/.claude/projects/*/memory/`), symlink it to your versioned knowledge directory:
```bash
rm -rf ~/.claude/projects/-Users-.../memory
ln -s /path/to/repo/knowledge/my-index ~/.claude/projects/-Users-.../memory
```
The runtime's retrieval still works (reads through symlink), but files live in git.

## Anti-patterns
- **Index too long**: If the index exceeds ~200 lines, it's eating too much context. Split into sub-indexes or prune stale entries.
- **Descriptions too vague**: "project notes" matches everything and nothing. Be specific: "auth middleware rewrite driven by compliance requirements."
- **Detail files too large**: Each file should be focused enough to load without wasting context. Split if over ~100 lines.
- **No index at all**: A directory of 50 files with no index means either loading everything (expensive) or loading nothing (useless).

## Outcome
- Knowledge scales without bloating the context window
- Retrieval is transparent — you can read the index and understand what's available
- Git versioning means knowledge is durable and auditable
- Pattern is runtime-agnostic — works with Claude Code, gptme, Codex, or custom tooling

## Origin
2026-04-13: Discovered while integrating Claude Code's auto-memory with Alice's git-based brain repo. Claude's memory system is exactly this pattern (MEMORY.md index + detail files with description-based retrieval). Generalized for any LLM knowledge base.
