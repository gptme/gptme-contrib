# gptme-rag

ChromaDB-based RAG (Retrieval-Augmented Generation) for gptme agents.

Part of [gptme-contrib](https://github.com/gptme/gptme-contrib). Upstreamed from [gptme/gptme-rag](https://github.com/gptme/gptme-rag).

Enhances AI responses by retrieving and incorporating relevant context from your local files using vector/semantic search with ChromaDB.

This is the **vector search** complement to [`gptme-wisdom`](https://github.com/gptme/gptme-contrib/tree/master/packages/gptme-wisdom) (BM25/SQLite exact-term search). Different approaches for different use cases.

## Features

- 📚 Document indexing with ChromaDB (vector storage, semantic search, persistence)
- 🔍 Semantic search with sentence-transformers or OpenRouter API embeddings
- 🔎 Lexical (TF-IDF) retrieval for exact-identifier queries (`gptme-rag[lexical]`)
- 📄 Smart document processing (streaming, chunking, reconstruction)
- 👀 File watching and auto-indexing
- 🔌 MCP server for agent integration (`gptme-rag mcp`)
- 🛠️ CLI interface (`gptme-rag index`, `gptme-rag search`)
- 📊 Injection logging and index-health/rot reporting (`gptme_rag.observability`)
- 🧠 Knowledge-entry source over gptme's JSONL store (`KnowledgeEntrySource`, `memory_type="knowledge_entry"`)

## Quick Start

```bash
# Index your documents
gptme-rag index /path/to/documents

# Index with API embeddings instead of local CPU embeddings
OPENROUTER_API_KEY=... gptme-rag index /path/to/documents --embedding-function openrouter

# Search with semantic relevance
gptme-rag search "your query"

# Start MCP server (for agent tool integration)
gptme-rag mcp --persist-dir /path/to/index
```

OpenRouter embedding model defaults to `openai/text-embedding-3-large` and can be
overridden with `OPENROUTER_EMBEDDING_MODEL`. If `--embedding-function
openrouter` is requested without an API key, `gptme-rag` falls back to the local
ModernBERT embedding backend.

Local (sentence-transformers) embeddings are cached by chunk content hash in
`~/.cache/gptme-rag/local-embeddings.sqlite` (`XDG_CACHE_HOME` respected).
Change detection is per file, so an appended line re-submits every chunk of
that file; the cache turns the unchanged chunks into lookups instead of CPU
re-embeds. Set `GPTME_RAG_EMBEDDING_CACHE=/path/to/cache.sqlite` to relocate it
or `GPTME_RAG_EMBEDDING_CACHE=off` to disable.

## Development

```bash
# Run tests (excluding slow embedding-model tests)
uv run pytest packages/gptme-rag/ -v -m "not slow"

# Run all tests
uv run pytest packages/gptme-rag/ -v
```

## License

MIT
