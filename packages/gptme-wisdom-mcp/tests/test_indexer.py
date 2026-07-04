"""Basic smoke tests for gptme-wisdom-mcp indexer and parsers."""

import json
import tempfile
from datetime import datetime
from pathlib import Path


def test_session_index_roundtrip():
    """Create an index, add a document, search, find it."""
    from gptme_wisdom_mcp.indexer import SessionIndex
    from gptme_wisdom_mcp.parsers import SessionDocument

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.db"
        with SessionIndex(db_path=db) as idx:
            doc = SessionDocument(
                source="journal",
                path="/tmp/test/2026-01-01/session.md",
                date=datetime(2026, 1, 1),
                title="Test session about Python async",
                content="We fixed a race condition in the async event loop using asyncio locks.",
                summary="Fixed async race condition",
                metadata={"model": "test-model", "tokens": 123},
            )
            assert idx.add(doc) is True
            # Duplicate should return False
            assert idx.add(doc) is False

            results = idx.search("async race condition")
            assert len(results) == 1
            assert results[0]["title"] == "Test session about Python async"
            stored_metadata = idx.conn.execute(
                "SELECT metadata FROM sessions WHERE path = ?", (doc.path,)
            ).fetchone()[0]
            assert json.loads(stored_metadata) == {"model": "test-model", "tokens": 123}


def test_purge_rebuild_preserves_content_search():
    """Purging one path should not degrade FTS recall for remaining content."""
    from gptme_wisdom_mcp.indexer import SessionIndex
    from gptme_wisdom_mcp.parsers import SessionDocument

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.db"
        with SessionIndex(db_path=db) as idx:
            kept = SessionDocument(
                source="journal",
                path="/tmp/test/2026-01-01/keep.md",
                date=datetime(2026, 1, 1),
                title="Kept session",
                content="This body contains the unique term zirconium-retrospective.",
                summary="A short summary without the body-only term",
            )
            removed = SessionDocument(
                source="journal",
                path="/tmp/test/2026-01-01/remove.md",
                date=datetime(2026, 1, 1),
                title="Removed session",
                content="This body contains throwaway deletion text.",
                summary="Removed summary",
            )
            assert idx.add(kept) is True
            assert idx.add(removed) is True

            assert idx.purge_paths_matching("remove.md") == 1
            results = idx.search("zirconium retrospective")
            assert len(results) == 1
            assert results[0]["path"] == kept.path


def test_session_search_handles_unbalanced_quote_query():
    """Malformed FTS quote syntax should fall back without crashing."""
    from gptme_wisdom_mcp.indexer import SessionIndex
    from gptme_wisdom_mcp.parsers import SessionDocument

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.db"
        with SessionIndex(db_path=db) as idx:
            doc = SessionDocument(
                source="journal",
                path="/tmp/test/2026-01-01/quoted.md",
                date=datetime(2026, 1, 1),
                title="Quoted session",
                content="He said hello while debugging the MCP search fallback.",
                summary="Debugged quote handling",
            )
            assert idx.add(doc) is True

            results = idx.search('He said "hello')
            assert len(results) == 1
            assert results[0]["path"] == doc.path


def test_book_index_roundtrip():
    """Create a book index, add a chunk, search, find it."""
    from gptme_wisdom_mcp.indexer import BookIndex
    from gptme_wisdom_mcp.parsers import BookDocument

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "wisdom.db"
        with BookIndex(db_path=db) as idx:
            doc = BookDocument(
                source="sicp",
                title="Structure and Interpretation of Computer Programs",
                chapter="1 Building Abstractions with Procedures",
                section="1.1 The Elements of Programming",
                content="A powerful programming language is more than just a means of instructing a computer to perform tasks.",
                url="https://mitpress.mit.edu/sicp/",
                license="CC BY-SA 4.0",
            )
            assert idx.add(doc) is True
            assert idx.add(doc) is False  # duplicate

            results = idx.search("programming language")
            assert len(results) == 1
            assert results[0]["source"] == "sicp"
            sources = idx.sources()
            assert len(sources) == 1
            assert sources[0]["source"] == "sicp"


def test_parse_book_text_basic():
    """parse_book_text should chunk content and return BookDocuments."""
    from gptme_wisdom_mcp.parsers import parse_book_text

    text = """# Chapter 1: Introduction

This is a foundational chapter covering the basics of computer science.
It introduces abstraction and the key building blocks of programs.
Programs are constructed from procedures that transform data.

## Section 1.1: Elements

The elements of programming are expressions, names, and procedures.
Every expression evaluates to a value in the language.
"""
    docs = parse_book_text(
        text,
        source="test-book",
        title="Test Book",
        url="https://example.com",
        target_tokens=50,
        min_chunk_tokens=5,  # small to accommodate short test text
    )
    assert len(docs) >= 1
    assert all(d.source == "test-book" for d in docs)
    assert all(d.title == "Test Book" for d in docs)


def test_mcp_server_importable():
    """mcp_server module should import without error."""
    import gptme_wisdom_mcp.mcp_server  # noqa: F401
    from gptme_wisdom_mcp.mcp_server import search_wisdom, search_sessions, list_wisdom_sources

    assert callable(search_wisdom)
    assert callable(search_sessions)
    assert callable(list_wisdom_sources)
