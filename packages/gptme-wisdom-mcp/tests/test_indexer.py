"""Basic smoke tests for gptme-wisdom-mcp indexer and parsers."""

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
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


def _cc_line(kind: str, text: str, ts: str) -> str:
    content = text if kind == "user" else [{"type": "text", "text": text}]
    return json.dumps(
        {"type": kind, "timestamp": ts, "message": {"role": kind, "content": content}}
    )


def test_grown_session_is_reindexed(tmp_path: Path):
    """A session indexed mid-write picks up later turns on the next run (cron)."""
    from gptme_wisdom_mcp.indexer import SessionIndex, index_all_sessions

    projects = tmp_path / "projects"
    f = projects / "-home-u-repo" / "abc.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text(_cc_line("user", "start the migration", "2026-01-01T00:00:00Z") + "\n")
    with SessionIndex(tmp_path / "s.db") as idx:
        assert index_all_sessions(idx, cc_projects_dir=projects)["claude_code"] == 1
        # Unchanged file: nothing to do
        assert index_all_sessions(idx, cc_projects_dir=projects)["claude_code"] == 0

        with f.open("a") as fh:
            fh.write(
                _cc_line("assistant", "root cause was a zeppelin deadlock", "2026-01-01T01:00:00Z")
                + "\n"
            )
        assert index_all_sessions(idx, cc_projects_dir=projects)["claude_code"] == 1

        assert len(idx.search("zeppelin")) == 1
        # The earlier content is still found, once (old FTS row was replaced)
        assert len(idx.search("migration")) == 1
        assert idx.count() == 1


def test_changed_document_drops_stale_terms(tmp_path: Path):
    """Re-indexing removes terms that are no longer in the document."""
    from gptme_wisdom_mcp.indexer import SessionIndex
    from gptme_wisdom_mcp.parsers import SessionDocument

    def doc(content: str) -> SessionDocument:
        return SessionDocument(
            source="journal",
            path="/journal/2026-01-01/session.md",
            date=datetime(2026, 1, 1),
            title="Session",
            content=content,
            summary="summary",
        )

    with SessionIndex(tmp_path / "s.db") as idx:
        assert idx.add(doc("draft mentions walrus")) is True
        assert idx.add(doc("final mentions narwhal")) is True
        assert idx.search("walrus") == []
        assert len(idx.search("narwhal")) == 1
        # The FTS index stays consistent with the stored rows
        idx.conn.execute(
            "INSERT INTO sessions_fts (sessions_fts, rank) VALUES ('integrity-check', 1)"
        )


def test_overlapping_updates_do_not_delete_stale_fts_values(tmp_path: Path):
    """A second connection cannot update between the stored-value read and write."""
    from gptme_wisdom_mcp.indexer import SessionIndex
    from gptme_wisdom_mcp.parsers import SessionDocument

    def doc(content: str) -> SessionDocument:
        return SessionDocument(
            source="journal",
            path="/journal/session.md",
            date=datetime(2026, 1, 1),
            title="Session",
            content=content,
            summary="summary",
        )

    with SessionIndex(tmp_path / "s.db") as first, SessionIndex(tmp_path / "s.db") as second:
        assert first.add(doc("walrus")) is True
        second.conn.execute("PRAGMA busy_timeout=0")
        concurrent_results = []
        attempted = False

        def interleave_after_read(cursor, row):
            nonlocal attempted
            stored = sqlite3.Row(cursor, row)
            if not attempted:
                attempted = True
                # The first connection has already read the old FTS values.
                # Let another real connection try to replace them before it writes.
                try:
                    concurrent_results.append(second.add(doc("narwhal")))
                except sqlite3.OperationalError as exc:
                    assert "locked" in str(exc)
            return stored

        first.conn.row_factory = interleave_after_read
        assert first.add(doc("narwhal")) is True
        first.conn.row_factory = sqlite3.Row
        assert attempted
        if not concurrent_results:
            # A serialized writer retries after the first transaction commits.
            concurrent_results.append(second.add(doc("narwhal")))
        assert concurrent_results == [False]
        assert first.count() == 1
        assert first.search("walrus") == []
        assert len(first.search("narwhal")) == 1
        assert first.add(doc("dolphin")) is True
        assert first.search("narwhal") == []
        assert len(first.search("dolphin")) == 1
        first.conn.execute(
            "INSERT INTO sessions_fts (sessions_fts, rank) VALUES ('integrity-check', 1)"
        )


def test_add_joins_a_transaction_already_open_on_the_connection(tmp_path: Path):
    """DML on idx.conn leaves a transaction open; add() must work and commit it."""
    from gptme_wisdom_mcp.indexer import SessionIndex
    from gptme_wisdom_mcp.parsers import SessionDocument

    doc = SessionDocument(
        source="journal",
        path="/journal/session.md",
        date=datetime(2026, 1, 1),
        title="Session",
        content="walrus",
        summary="summary",
    )

    with SessionIndex(tmp_path / "s.db") as idx:
        idx.conn.execute("INSERT INTO index_state (key, value) VALUES ('k', 'v')")
        assert idx.conn.in_transaction
        assert idx.add(doc) is True
        assert not idx.conn.in_transaction

    with SessionIndex(tmp_path / "s.db") as reopened:
        assert len(reopened.search("walrus")) == 1
        assert reopened.conn.execute("SELECT value FROM index_state").fetchone()[0] == "v"


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


def test_session_search_handles_timezone_aware_dates():
    """Timezone-aware indexed dates should not crash recency scoring."""
    from gptme_wisdom_mcp.indexer import SessionIndex
    from gptme_wisdom_mcp.parsers import SessionDocument

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.db"
        with SessionIndex(db_path=db) as idx:
            doc = SessionDocument(
                source="journal",
                path="/tmp/test/2026-01-01/aware.md",
                date=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
                title="Timezone-aware session",
                content="Timezone-aware session metadata should still be searchable.",
                summary="Timezone-aware metadata",
            )
            assert idx.add(doc) is True

            results = idx.search("timezone aware")
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
