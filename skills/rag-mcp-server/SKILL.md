---
name: rag-mcp-server
description: >
  Add foundational CS knowledge (7 classic books) and session-memory search to
  Claude Code or Claude Desktop via MCP. Lets Claude answer "how does X work?" by
  querying indexed books inline, and "what did we discuss about Y?" by searching
  past sessions. Use this skill when setting up or troubleshooting the rag-mcp-server.
license: MIT
compatibility: "Requires Python 3.10+, uv; books must be indexed before first use"
metadata:
  author: bob
  version: "1.0.0"
  tags: [mcp, rag, knowledge, search, sessions, wisdom]
  requires_tools: [uv]
  requires_skills: []
---

# rag-mcp-server Skill

Expose a local BM25 knowledge-base and session index to any MCP client — Claude
Code, Claude Desktop, or any gptme instance — so queries like "how does virtual
memory work?" resolve to book chapters inline, and "what did we discuss about
caching last week?" resolve to past sessions.

## Overview

`rag-mcp-server` is part of the `rag` package in your gptme agent workspace. It exposes
two search surfaces as MCP tools:

| Tool | What it searches |
|------|-----------------|
| `search_wisdom` | BM25 index over CC-licensed CS reference books |
| `list_wisdom_sources` | List of book slugs currently indexed |
| `search_sessions` | Journal, gptme logs, and Claude Code trajectory index |

Default book set (7): SICP, OSTEP, Pro Git, Think Python, MML Book, RL Intro,
Eloquent JavaScript. BM25 precision@3: 0.87 on a curated benchmark set.

## Setup

### 1. Install the package

```bash
# From gptme-contrib workspace root
uv sync --all-packages

# Verify the entry point is available
rag-mcp-server --help
```

### 2. Build the book (wisdom) index

```bash
# Download and index CC-licensed books in one command:
bob-search index-books --preset cs-fundamentals

# Preview without downloading:
bob-search index-books --preset cs-fundamentals --dry-run

# List available presets:
bob-search index-books --list-presets

# For books requiring manual download (follow --dry-run instructions, then):
#   gptme-wisdom ingest --source sicp --file ~/books/sicp.md
# Supported manual-ingest slugs: sicp, ostep, pro-git, thinkpython, mml-book,
#   rl-intro, eloquentjs
```

Default wisdom DB: `~/.local/share/gptme/wisdom.db`

### 3. Build the session index (optional)

```bash
bob-search index    # indexes journal/, gptme logs, Claude Code trajectories
```

Default session DB: `~/.local/share/bob/session-index.db`

### 4. Run a quick smoke test

```bash
# Should return book chunks about processes:
rag-mcp-server &
# In another shell or via MCP client:
# search_wisdom("virtual memory", source="ostep", top_k=2)

kill %1  # stop the background server
```

## MCP Configuration

### Claude Code

Add to `.claude/settings.json` in your project (or `~/.claude/settings.json`
for global use):

```json
{
  "mcpServers": {
    "wisdom-rag": {
      "command": "rag-mcp-server",
      "type": "stdio"
    }
  }
}
```

Or add it from the terminal:

```bash
claude mcp add wisdom-rag rag-mcp-server
```

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "wisdom-rag": {
      "command": "rag-mcp-server",
      "args": []
    }
  }
}
```

### Custom DB paths (non-default install)

```json
{
  "mcpServers": {
    "wisdom-rag": {
      "command": "rag-mcp-server",
      "args": [
        "--wisdom-db", "/path/to/wisdom.db",
        "--sessions-db", "/path/to/sessions.db"
      ]
    }
  }
}
```

## Tools Reference

### `search_wisdom`

```
search_wisdom(query: str, source?: str, top_k?: int = 5) -> list[Chunk]
```

Searches the indexed book corpus. Each result includes: `source` (book slug),
`title`, `chapter`, `content` (excerpt), `score`.

**Example queries** that work well:
- `"how does virtual memory work"` → OSTEP Chapter 13
- `"git rebase vs merge"` → Pro Git
- `"reinforcement learning q-learning"` → RL Intro
- `"higher-order functions in JavaScript"` → Eloquent JS
- `"backpropagation gradient descent"` → MML Book

### `list_wisdom_sources`

```
list_wisdom_sources() -> list[str]
```

Returns book slugs currently indexed (e.g. `["ostep", "sicp", "pro-git", ...]`).
Use to verify what's available before a query.

### `search_sessions`

```
search_sessions(query: str, source?: str, limit?: int = 5) -> list[Session]
```

Searches past sessions (journal, gptme logs, Claude Code trajectories). Each
result includes: `source`, `date`, `title`, `summary`, `path`, `score`.

`source` can be `journal`, `gptme`, or `claude_code`.

**Example queries**:
- `"RAG retrieval implementation"` → sessions where RAG was discussed
- `"bug in websocket reconnect"` → debugging sessions from the past
- `"how did we approach the authentication refactor"` → design sessions

## Usage Examples

Once configured, Claude calls these tools automatically during conversation:

```
User: How does copy-on-write work in operating systems?
Claude: [calls search_wisdom("copy-on-write operating system", source="ostep")]
        → OSTEP §21: "…when a child forks, pages are shared read-only;
           a write triggers a private copy…"
```

```
User: What did we implement for the session recording last week?
Claude: [calls search_sessions("session recording implementation", limit=3)]
        → Journal 2026-06-28: "Implemented incremental re-index…"
```

## Troubleshooting

**`rag-mcp-server` not found**: run `uv sync --all-packages` from the
gptme-contrib root, then verify with `which rag-mcp-server`.

**`No wisdom sources indexed`**: run `bob-search index-books --preset
cs-fundamentals` to build the initial wisdom DB.

**`search_sessions` returns empty**: run `bob-search index` to build the
session index. Journal-only installs only need the journal path to exist.

**MCP server unreachable in Claude Code**: confirm the `rag-mcp-server` command
is on PATH in the shell Claude Code uses (try `claude mcp list` to verify the
server is registered and `claude mcp get wisdom-rag` to see its status).

## Related

- Package source: `packages/rag/` in your gptme agent workspace
- Full docs: `packages/rag/README.md` in your gptme agent workspace
- MCP specification: [modelcontextprotocol.io](https://modelcontextprotocol.io/)
- `gptme-wisdom` ingest CLI (upstream): `gptme-wisdom --help`
