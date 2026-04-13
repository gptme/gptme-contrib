---
match:
  keywords:
    - knowledge base
    - memory index
    - auto-memory
    - MEMORY.md
    - symlink memory
    - knowledge retrieval
status: active
---

# Symlink Runtime Memory Into Version-Controlled Knowledge Base

## Rule
When a runtime has a local memory/knowledge system with useful retrieval (indexes, relevance matching), symlink its storage into a version-controlled knowledge base rather than choosing one or the other.

## Context
Some AI runtimes maintain local memory systems with useful properties:
- **Claude Code**: `~/.claude/projects/*/memory/` with `MEMORY.md` index, description-based relevance matching, structured frontmatter
- **Other runtimes** may have similar per-project knowledge stores

These are convenient but fragile — not versioned, not portable, invisible to other runtimes. Meanwhile, git-based knowledge bases (`knowledge/`, wikis, docs/) are durable but lack the auto-retrieval.

The insight: the retrieval mechanism and the storage location are independent. You can keep the retrieval by symlinking the storage.

## Pattern

### Setup
```bash
# 1. Create the knowledge dir in your repo
mkdir -p knowledge/claude-memory

# 2. Move existing memory files into it
cp ~/.claude/projects/-Users-.../memory/* knowledge/claude-memory/

# 3. Replace local dir with symlink
rm -rf ~/.claude/projects/-Users-.../memory
ln -s /path/to/repo/knowledge/claude-memory \
      ~/.claude/projects/-Users-.../memory
```

### How it works
- **Writes** through the symlink land in the repo → `git add` and commit them
- **Reads** by the runtime resolve through the symlink → retrieval still works
- **Index files** (like `MEMORY.md`) are loaded by the runtime from the symlink path, unaware anything changed

### The index pattern
The key design element is the **index file** — a compact manifest loaded into every conversation:
```markdown
# Memory Index

## Topic A
- [detail-a.md](detail-a.md) — one-line description used for relevance matching

## Topic B
- [detail-b.md](detail-b.md) — another description
```

The index is cheap to load (always in context). Individual files are loaded selectively based on descriptions matching the current conversation. This is a general pattern for any knowledge base that needs to be queryable by an LLM — not specific to Claude Code.

### Applying this pattern to other knowledge bases
Any directory of markdown files with an index can use this pattern:
1. **Index file**: compact, always loaded, contains pointers + one-line descriptions
2. **Detail files**: structured content with frontmatter (name, description, type)
3. **Selective loading**: match descriptions against current context to decide what to load
4. **Version control**: the whole directory is in git

## Outcome
- Runtime's retrieval mechanism works unchanged
- Knowledge is versioned and reviewable via git
- Knowledge is portable across machines (clone the repo)
- Other runtimes can read the same knowledge directory
- No trade-off — you get both convenience and durability

## Origin
2026-04-13: Alice was saving knowledge to Claude Code's local memory instead of her brain repo. Symlinked the memory directory into the repo. Claude Code's auto-retrieval still works, files are now in git. Erik asked to document the general pattern.
