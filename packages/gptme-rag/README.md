# gptme-rag

ChromaDB-based RAG (Retrieval-Augmented Generation) for gptme agents.

Part of [gptme-contrib](https://github.com/gptme/gptme-contrib). Upstreamed from [gptme/gptme-rag](https://github.com/gptme/gptme-rag).

Enhances AI responses by retrieving and incorporating relevant context from your local files using vector/semantic search with ChromaDB.

This is the **vector search** complement to [`gptme-wisdom`](https://github.com/gptme/gptme-contrib/tree/master/packages/gptme-wisdom) (BM25/SQLite exact-term search). Different approaches for different use cases.

## Features

- 📚 Document indexing with ChromaDB (vector storage, semantic search, persistence)
- 🔍 Semantic search with sentence-transformers embeddings
- 📄 Smart document processing (streaming, chunking, reconstruction)
- 👀 File watching and auto-indexing
- 🔌 MCP server for agent integration (`gptme-rag mcp`)
- 🛠️ CLI interface (`gptme-rag index`, `gptme-rag search`)

## Quick Start

```bash
# Index your documents
gptme-rag index /path/to/documents

# Search with semantic relevance
gptme-rag search "your query"

# Start MCP server (for agent tool integration)
gptme-rag mcp --persist-dir /path/to/index
```

## Development

```bash
# Run tests (excluding slow embedding-model tests)
uv run pytest packages/gptme-rag/ -v -m "not slow"

# Run all tests
uv run pytest packages/gptme-rag/ -v
```

## License

MIT
