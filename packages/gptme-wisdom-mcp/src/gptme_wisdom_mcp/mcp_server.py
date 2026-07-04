"""RAG-as-MCP Knowledge Server for gptme agents.

Exposes a wisdom index (BM25 over CS books) and a session search index as MCP
tools, so Claude Code, Claude Desktop, and any MCP-capable client can query
foundational knowledge and cross-session memory without running a separate script.

Usage:
    uv run gptme-wisdom-mcp                                       # stdio (default paths)
    uv run gptme-wisdom-mcp --wisdom-db ~/books/wisdom.db         # custom wisdom DB
    uv run gptme-wisdom-mcp --sessions-db ~/sessions/index.db     # custom sessions DB

Tools exposed:
    search_wisdom(query, source?, top_k?)   → list[Chunk]
    list_wisdom_sources()                   → list[Source]
    search_sessions(query, source?, limit?) → list[SessionResult]
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from gptme_wisdom_mcp.indexer import BookIndex, SessionIndex

# Module-level path overrides — set by main() before mcp.run() so tool functions
# pick up custom DB locations when the server is run with --wisdom-db / --sessions-db.
_wisdom_db: Path | None = None
_sessions_db: Path | None = None

mcp = FastMCP(
    "wisdom-rag",
    instructions=(
        "Search a knowledge base of foundational CS books and session memory. "
        "`search_wisdom` queries indexed books (SICP, OSTEP, Pro Git, etc.) via BM25. "
        "`search_sessions` searches cross-session journal memory. "
        "Use `list_wisdom_sources` to see which books are indexed."
    ),
)


@mcp.tool()
def search_wisdom(
    query: str,
    source: Optional[str] = None,
    top_k: int = 5,
) -> list[dict]:
    """Search the wisdom-layer book index via BM25.

    Args:
        query: Free-text search query. Supports multi-word phrases.
        source: Optional slug to restrict results (e.g. "sicp", "ostep", "pro-git").
                Use list_wisdom_sources() to see all available slugs.
        top_k: Number of results to return (1–20). Default 5.

    Returns:
        List of matching chunks, each with keys:
            source, title, chapter, section, content, url, license, page, score
    """
    top_k = max(1, min(20, top_k))
    with BookIndex(db_path=_wisdom_db) as idx:
        results = idx.search(query, source=source or None, limit=top_k)
    # Trim content to avoid blowing up context windows
    for r in results:
        if len(r.get("content", "")) > 800:
            r["content"] = r["content"][:800] + "…"
    return list(results)


@mcp.tool()
def list_wisdom_sources() -> list[dict]:
    """Return metadata for books currently indexed in the wisdom layer.

    Use each row's `source` slug as the `source` argument in search_wisdom() to
    restrict results to a specific book.
    """
    with BookIndex(db_path=_wisdom_db) as idx:
        return list(idx.sources())


@mcp.tool()
def search_sessions(
    query: str,
    source: Optional[str] = None,
    limit: int = 5,
) -> list[dict]:
    """Search cross-session journal and conversation memory via BM25 + recency.

    Args:
        query: Free-text query against session titles and summaries.
        source: Optional filter — "journal", "gptme", or "claude_code".
        limit: Number of results to return (1–20). Default 5.

    Returns:
        List of session records with keys:
            source, path, date, title, summary, score
    """
    limit = max(1, min(20, limit))
    with SessionIndex(db_path=_sessions_db) as idx:
        results = idx.search(
            query,
            source=source or None,
            limit=limit,
        )
    # Drop internal FTS fields that are noise for the caller
    clean = []
    for r in results:
        clean.append(
            {
                "source": r["source"],
                "date": r["date"],
                "title": r["title"],
                "summary": r["summary"][:400] + "…"
                if len(r.get("summary", "")) > 400
                else r.get("summary", ""),
                "path": r["path"],
                "score": round(r["score"], 4),
            }
        )
    return clean


def main() -> None:
    """Entry point for `gptme-wisdom-mcp` script."""
    global _wisdom_db, _sessions_db

    parser = argparse.ArgumentParser(
        description="RAG-as-MCP Knowledge Server — expose wisdom books and session memory as MCP tools."
    )
    parser.add_argument(
        "--wisdom-db",
        type=Path,
        default=None,
        help="Path to the wisdom BM25 SQLite DB (default: ~/.local/share/gptme/wisdom.db)",
    )
    parser.add_argument(
        "--sessions-db",
        type=Path,
        default=None,
        help="Path to the session index SQLite DB (default: ~/.local/share/gptme/session-index.db)",
    )
    args = parser.parse_args()

    if args.wisdom_db:
        _wisdom_db = args.wisdom_db.expanduser().resolve()
    if args.sessions_db:
        _sessions_db = args.sessions_db.expanduser().resolve()

    mcp.run()


if __name__ == "__main__":
    main()
