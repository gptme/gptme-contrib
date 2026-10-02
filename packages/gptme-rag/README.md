# gptme-rag

Local semantic search over your files for AI agents: index directories into ChromaDB with local
(sentence-transformers) or API embeddings, then query them from the CLI, from gptme's built-in
`rag` tool, or from any MCP client.

**Status:** beta. Upstreamed from the now-archived
[gptme/gptme-rag](https://github.com/gptme/gptme-rag) and developed here. The `gptme-rag` package
on PyPI is an old release from that repo — install from this repository for current features.

## Why / when to use it

Use gptme-rag when an agent needs to find relevant passages in a large body of local text (docs,
notes, code, a workspace's knowledge base) by meaning rather than exact words. It is the
**vector-search** complement to [gptme-wisdom](../gptme-wisdom/README.md) (BM25/SQLite, good for
exact terms over reference books). For structural code navigation see
[gptme-codegraph](../gptme-codegraph/README.md).

How it fits with gptme:

- gptme core's `rag` tool calls the `gptme-rag` CLI when it is on `PATH` and `[rag] enabled = true`
  is set in `gptme.toml` ([gptme docs](https://gptme.org/docs/tools.html)).
- `gptme-rag mcp` serves the same index to Claude Code, Cursor, Codex, gptme or any MCP client.
- [gptme-retrieval](../../plugins/gptme-retrieval/) is a gptme plugin for automatic context
  retrieval.

## Install

```bash
# CLI (local embeddings pull in sentence-transformers/torch — a large install)
uv tool install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-rag"

# With the MCP server and lexical (TF-IDF) extras
uv tool install "gptme-rag[mcp,lexical] @ git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-rag"
```

Extras: `mcp` (MCP server), `lexical` (scikit-learn TF-IDF backend).

## Quickstart

```bash
gptme-rag index ~/notes ~/projects/docs          # build/update the index
gptme-rag search "how do we rotate credentials"  # semantic search
gptme-rag status                                 # what's in the default index
```

The default index lives in `~/.cache/gptme/rag`; pass `--persist-dir` to `index`, `search`,
`watch`, `clean`, `gc-orphans` and `mcp` to use another one. Re-indexing is incremental (per-file
change detection).

## Commands

| Command | Description |
|---------|-------------|
| `index PATHS... [-p GLOB]` | Index files (default glob `**/*.*`). Options: `--embedding-function`, `--device cpu\|cuda`, `--chunk-size`, `--chunk-overlap`, `--file-limit` (per directory, default 100000), `--force-recreate` |
| `search QUERY [PATHS...]` | Search and assemble context. Options: `-n/--n-results`, `--max-tokens`, `--format summary\|full`, `--expand none\|adjacent\|file`, `-f/--filter GLOB` (repeatable), `--score`, `--explain`, `--weights JSON`, `--json`, `--raw` |
| `watch DIR` | Index `DIR` and keep the index updated as files change (`--ignore-patterns`) |
| `status` | Document/chunk counts and source breakdown for the default index |
| `clean [--force]` | Delete the index directory |
| `gc-orphans [--apply]` | List (or delete) leftover Chroma segment directories not referenced by the index |
| `mcp` | Run an MCP stdio server (needs the `mcp` extra) |
| `benchmark indexing\|search-benchmark\|watch-perf DIR` | Performance benchmarks |

Global flag: `-v/--verbose`.

## Embeddings

`--embedding-function` accepts `auto` (default — reuse whatever model an existing collection was
built with), `modernbert`, `minilm`, `mpnet`, `openrouter` or `default`.

- **Local** (sentence-transformers, CPU by default). Embeddings are cached by chunk content hash
  in `~/.cache/gptme-rag/local-embeddings.sqlite` (respects `XDG_CACHE_HOME`), so re-indexing a
  file where one line changed doesn't re-embed every chunk. Set
  `GPTME_RAG_EMBEDDING_CACHE=/path/to/cache.sqlite` to move it, or `=off` to disable.
- **OpenRouter API**: `OPENROUTER_API_KEY=... gptme-rag index DIR --embedding-function openrouter`.
  Model defaults to `openai/text-embedding-3-large`; override with `OPENROUTER_EMBEDDING_MODEL`.
  Without an API key it falls back to the local ModernBERT backend.

Switching models on an existing index requires `--force-recreate`.

## MCP server

```bash
gptme-rag mcp --persist-dir ~/.cache/gptme/rag
# e.g. register with Claude Code:
claude mcp add gptme-rag -- gptme-rag mcp
```

Tools: `rag_query(query, top_k=5)` (max 50), `rag_index_status()`,
`rag_index_refresh(directory, pattern="**/*.*")`. Each accepts an optional `persist_dir`.

## Python API

Beyond the CLI, the package exposes building blocks for agents that assemble their own retrieval:

- `gptme_rag.Indexer`, `gptme_rag.ContextAssembler` — the dense index and context assembly
  (lazy-loaded; need the full dependencies).
- `gptme_rag.lexical.TfidfIndex` — TF-IDF backend for exact-identifier queries (function names,
  paths, error strings) where embeddings are weak; needs the `lexical` extra.
- `gptme_rag.lesson_matcher` — `scan_lessons`, `score_lessons`, `filter_by_session_category`:
  keyword/wildcard + BM25 lesson scoring, compatible with gptme's lesson format.
- `gptme_rag.sources` — declarative source registry for collecting a reproducible corpus.
- `gptme_rag.knowledge_source.KnowledgeEntrySource` — indexes the store written by
  `gptme-util knowledge save` as `memory_type="knowledge_entry"` documents.
- `gptme_rag.memory_type` — caller-supplied rules for classifying documents (identity, project,
  preference, …) and boosting matching results.
- `gptme_rag.observability` — injection logging and index-health ("is my index stale?") checks.

## Development

```bash
uv run pytest packages/gptme-rag/ -v -m "not slow"   # skip tests that load embedding models
uv run pytest packages/gptme-rag/ -v                 # everything
```

## License

MIT
