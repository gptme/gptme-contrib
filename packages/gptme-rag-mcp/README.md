# gptme-rag-mcp

RAG-as-MCP Knowledge Server for gptme agents — search foundational CS books and
AI session history via the Model Context Protocol (MCP).

## Prior art and consolidation

This package is related to the existing
[`gptme/gptme-rag`](https://github.com/gptme/gptme-rag) project, which already
provides ChromaDB-backed semantic document RAG, file watching, CLI commands, and
an MCP server. This contrib package is a narrower MCP surface for the
`gptme-wisdom` book index and Bob/gptme session search.

If `gptme-rag` is upstreamed into `gptme-contrib`, this package should be
reconciled with that repo instead of replacing or reimplementing its existing
indexing stack.

## What it does

`gptme-rag-mcp` exposes two knowledge planes as MCP tools:

| Tool | What it searches |
|------|-----------------|
| `search_wisdom(query)` | Indexed CS books (SICP, OSTEP, Pro Git, …) via BM25 |
| `list_wisdom_sources()` | Lists which books are currently indexed |
| `search_sessions(query)` | gptme journals, Claude Code transcripts, conversation logs |

## Install

```bash
uv tool install gptme-rag-mcp
# or
pip install gptme-rag-mcp
```

## Quick start

```bash
# Run on stdio (default MCP transport)
rag-mcp-server

# Custom DB paths
rag-mcp-server --wisdom-db ~/books/wisdom.db --sessions-db ~/sessions/index.db
```

## Claude Desktop config

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "wisdom-rag": {
      "command": "rag-mcp-server",
      "args": [
        "--wisdom-db", "/path/to/wisdom.db",
        "--sessions-db", "/path/to/session-index.db"
      ]
    }
  }
}
```

## Claude Code config

Add to `.claude/settings.json` in your project:

```json
{
  "mcpServers": {
    "wisdom-rag": {
      "command": "rag-mcp-server",
      "args": ["--wisdom-db", "/path/to/wisdom.db"]
    }
  }
}
```

Or via `claude mcp add`:

```bash
claude mcp add wisdom-rag -- rag-mcp-server --wisdom-db /path/to/wisdom.db
```

## Building the wisdom index

The wisdom DB is populated separately by indexing CC-licensed books.
Use [`gptme-wisdom`](../gptme-wisdom/) to ingest supported books:

```bash
gptme-wisdom ingest --source thinkpython --file /tmp/thinkpython.txt
gptme-wisdom search "recursion base case"
```

## Building the session index

The session index is built from gptme conversation logs, Claude Code transcripts,
and journal markdown files. Point the server at your existing index:

```bash
rag-mcp-server --sessions-db ~/.local/share/gptme/session-index.db
```

## Acknowledgments

Built on top of the [gptme](https://github.com/gptme/gptme) agent framework.
Session indexing uses SQLite FTS5 (BM25) — zero external dependencies beyond
the `mcp` package.
