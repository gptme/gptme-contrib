# gptme-rag

RAG-as-MCP Knowledge Server for gptme agents — search foundational CS books and
AI session history via the Model Context Protocol (MCP).

## What it does

`gptme-rag` exposes two knowledge planes as MCP tools:

| Tool | What it searches |
|------|-----------------|
| `search_wisdom(query)` | Indexed CS books (SICP, OSTEP, Pro Git, …) via BM25 |
| `list_wisdom_sources()` | Lists which books are currently indexed |
| `search_sessions(query)` | gptme journals, Claude Code transcripts, conversation logs |

## Install

```bash
uv tool install gptme-rag
# or
pip install gptme-rag
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
See the `rag-mcp-server index` subcommand (coming soon) or use the
[`rag` package](https://github.com/ErikBjare/bob/tree/master/packages/rag)
directly for the full indexing pipeline.

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
