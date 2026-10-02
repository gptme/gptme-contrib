# gptme-wisdom-mcp

MCP server that lets any MCP client (gptme, Claude Code, Claude Desktop, …)
search a local index of reference books **and** your past agent sessions —
journals, gptme logs, Claude Code / Codex / Cursor transcripts — with BM25
full-text search over SQLite FTS5.

**Status:** experimental (alpha, `0.1.0`). Small, read-only, stdio-only.

## Why / when to use it

Use it when you want an agent to look things up in two local knowledge planes
without shelling out to a script:

- **Wisdom** — chunks of canonical CS books (SICP, OSTEP, Pro Git, …) that you
  have ingested with [`gptme-wisdom`](../gptme-wisdom/README.md). Good for
  primary-source answers that web search tends to bury.
- **Sessions** — cross-session memory: "did we already fix this?", "what did
  the journal say about X last month?".

Both indexes are plain SQLite files. Keyword search only — no embeddings, no
model downloads.

How it relates to sibling packages:

| Package | Role |
|---------|------|
| [`gptme-wisdom`](../gptme-wisdom/README.md) | Builds and queries the book index (CLI). This server reads the same DB. |
| [`gptme-rag`](../gptme-rag/README.md) | Vector/semantic search over arbitrary documents (ChromaDB), with its own MCP server. Pick it when you need semantic matching rather than exact terms. |

## MCP tools

| Tool | Arguments | Returns |
|------|-----------|---------|
| `search_wisdom` | `query`, `source?` (book slug, e.g. `sicp`), `top_k?` (1–20, default 5) | Chunks with `source`, `title`, `chapter`, `section`, `content` (truncated to 800 chars), `url`, `license`, `page`, `score` |
| `list_wisdom_sources` | — | Metadata for each indexed book; use its `source` slug to filter `search_wisdom` |
| `search_sessions` | `query`, `source?` (`journal`, `gptme`, `claude_code`, `cursor`, `codex`), `limit?` (1–20, default 5) | Sessions with `source`, `date`, `title`, `summary` (truncated to 400 chars), `path`, `score` |

Session results are ranked by BM25 relevance blended with recency (90-day
half-life); journal entries get a 1.3× weight because they are curated
summaries rather than raw transcripts.

## Install

The package depends on `gptme-wisdom`, which lives in the same monorepo. The
most reliable install is from a checkout of
[gptme-contrib](https://github.com/gptme/gptme-contrib), installing both
packages together:

```bash
git clone https://github.com/gptme/gptme-contrib
cd gptme-contrib
pip install ./packages/gptme-wisdom ./packages/gptme-wisdom-mcp
```

Inside the uv workspace (contributors) you can instead run
`uv run --package gptme-wisdom-mcp gptme-wisdom-mcp` from the repo root (the
workspace root project does not depend on this package, so a bare `uv run`
there won't find the executable).

Either way you get a `gptme-wisdom-mcp` executable.

## Quickstart

1. Build a wisdom index (see [`gptme-wisdom`](../gptme-wisdom/README.md) for
   getting book text):

   ```bash
   gptme-wisdom ingest --source thinkpython --file /tmp/thinkpython.txt
   gptme-wisdom list
   ```

2. Start the server on stdio (normally your MCP client does this for you):

   ```bash
   gptme-wisdom-mcp
   # or with explicit DB paths
   gptme-wisdom-mcp --wisdom-db ~/books/wisdom.db --sessions-db ~/sessions/index.db
   ```

### Options

| Flag | Default |
|------|---------|
| `--wisdom-db PATH` | `~/.local/share/gptme/wisdom.db` (same default as `gptme-wisdom`) |
| `--sessions-db PATH` | `~/.local/share/gptme/session-index.db` |

A missing session DB is created empty on first use, so `search_sessions`
simply returns nothing until you index sessions (below).

## Client configuration

### gptme

In `~/.config/gptme/config.toml` (or a project `gptme.toml`):

```toml
[mcp]
enabled = true

[[mcp.servers]]
name = "wisdom-rag"
command = "gptme-wisdom-mcp"
args = ["--wisdom-db", "/path/to/wisdom.db"]
```

### Claude Code

```bash
claude mcp add wisdom-rag -- gptme-wisdom-mcp --wisdom-db /path/to/wisdom.db
```

### Claude Desktop

Add to `claude_desktop_config.json`
(`~/Library/Application Support/Claude/` on macOS, `%APPDATA%\Claude\` on
Windows):

```json
{
  "mcpServers": {
    "wisdom-rag": {
      "command": "gptme-wisdom-mcp",
      "args": [
        "--wisdom-db", "/path/to/wisdom.db",
        "--sessions-db", "/path/to/session-index.db"
      ]
    }
  }
}
```

## Building the session index

There is no CLI for this yet; use the Python API. `index_all_sessions` is
incremental (already-indexed paths are skipped), so it is safe to re-run from
a cron job or timer:

```python
from pathlib import Path
from gptme_wisdom_mcp.indexer import SessionIndex, index_all_sessions

with SessionIndex() as idx:  # default: ~/.local/share/gptme/session-index.db
    counts = index_all_sessions(
        idx,
        journal_dir=Path("~/my-agent/journal").expanduser(),        # YYYY-MM-DD*.md or YYYY-MM-DD/*.md
        gptme_logs_dir=Path("~/.local/share/gptme/logs").expanduser(),
        cc_projects_dir=Path("~/.claude/projects").expanduser(),    # Claude Code JSONL
        codex_home_dir=Path.home(),   # opt-in: reads ~/.codex/sessions/
        cursor_home_dir=Path.home(),  # opt-in: reads ~/.cursor/
        verbose=True,
    )
    print(counts)
```

Every source is optional — pass only the directories you have.

## Development

```bash
make test       # pytest
make typecheck  # mypy
```

## Prior art

[`gptme-rag`](../gptme-rag/README.md) (now part of this monorepo) provides
semantic document RAG with its own MCP server. This package is deliberately a
narrower, dependency-light surface for BM25 search over the `gptme-wisdom` book
index and session history; it does not reimplement `gptme-rag`'s indexing
stack.
