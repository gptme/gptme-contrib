"""Basic smoke tests for gptme-wisdom-mcp indexer and parsers."""

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest


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


def _assert_fts_matches_rows(idx) -> None:
    """The FTS index holds exactly the tokens of the stored rows.

    FTS5 'integrity-check' cannot show this: a content='' table has nothing to
    compare against. Compare every token instance with a freshly built index.
    """
    idx.conn.executescript(
        """
        CREATE VIRTUAL TABLE temp.fresh USING fts5(
            title, content, summary, content='', tokenize='porter unicode61'
        );
        INSERT INTO temp.fresh (rowid, title, content, summary)
            SELECT id, title, content, summary FROM sessions;
        CREATE VIRTUAL TABLE temp.live_terms
            USING fts5vocab('main', 'sessions_fts', 'instance');
        CREATE VIRTUAL TABLE temp.fresh_terms
            USING fts5vocab('temp', 'fresh', 'instance');
        """
    )
    live = sorted(map(tuple, idx.conn.execute("SELECT * FROM temp.live_terms")))
    fresh = sorted(map(tuple, idx.conn.execute("SELECT * FROM temp.fresh_terms")))
    idx.conn.executescript(
        """
        DROP TABLE temp.live_terms;
        DROP TABLE temp.fresh_terms;
        DROP TABLE temp.fresh;
        """
    )
    assert live == fresh


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
        _assert_fts_matches_rows(idx)


def test_date_or_metadata_change_alone_is_reindexed(tmp_path: Path):
    """A re-parse with identical text still refreshes date and metadata."""
    from gptme_wisdom_mcp.indexer import SessionIndex
    from gptme_wisdom_mcp.parsers import SessionDocument

    def doc(day: int, metadata: dict) -> SessionDocument:
        return SessionDocument(
            source="journal",
            path="/journal/session.md",
            date=datetime(2026, 1, day),
            title="Session",
            content="walrus",
            summary="summary",
            metadata=metadata,
        )

    with SessionIndex(tmp_path / "s.db") as idx:
        assert idx.add(doc(1, {})) is True
        assert idx.add(doc(1, {"model": "m"})) is True
        assert idx.add(doc(2, {"model": "m"})) is True
        assert idx.add(doc(2, {"model": "m"})) is False
        row = idx.conn.execute("SELECT date, metadata FROM sessions").fetchone()
        assert tuple(row) == ("2026-01-02T00:00:00", '{"model": "m"}')
        assert len(idx.search("walrus")) == 1
        _assert_fts_matches_rows(idx)


def test_session_without_timestamps_is_not_reindexed_every_run(tmp_path: Path):
    """The date fallback must be stable, or every cron run rewrites the row."""
    from gptme_wisdom_mcp.indexer import SessionIndex, index_all_sessions

    projects = tmp_path / "projects"
    f = projects / "-home-u-repo" / "abc.jsonl"
    f.parent.mkdir(parents=True)
    line = {"type": "user", "message": {"role": "user", "content": "start the migration"}}
    f.write_text(json.dumps(line) + "\n")
    with SessionIndex(tmp_path / "s.db") as idx:
        assert index_all_sessions(idx, cc_projects_dir=projects)["claude_code"] == 1
        assert index_all_sessions(idx, cc_projects_dir=projects)["claude_code"] == 0


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
        _assert_fts_matches_rows(first)


def test_a_grown_session_is_reindexed_inside_a_joined_transaction(tmp_path: Path):
    """The normal re-index path works when the caller already has a transaction."""
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

    with SessionIndex(tmp_path / "s.db") as idx:
        assert idx.add(doc("draft mentions walrus")) is True
        idx.conn.execute("INSERT INTO index_state (key, value) VALUES ('k', 'v')")
        assert idx.conn.in_transaction
        # A session that grew since it was indexed, added while the caller's
        # transaction is open: the savepoint path must still swap the FTS row.
        assert idx.add(doc("final mentions narwhal")) is True
        idx.conn.commit()

        assert idx.search("walrus") == []
        assert len(idx.search("narwhal")) == 1
        assert idx.count() == 1
        _assert_fts_matches_rows(idx)


def test_a_joined_transaction_cannot_write_from_a_stale_snapshot(tmp_path: Path):
    """The savepoint path fails loudly when another connection committed first.

    It takes no BEGIN IMMEDIATE, so the only thing protecting the stored-value
    read is WAL snapshot isolation: a write attempt against a superseded
    snapshot raises SQLITE_BUSY_SNAPSHOT instead of applying stale FTS values.
    """
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
        first.conn.execute("PRAGMA busy_timeout=0")
        # A deferred transaction that has only read: its snapshot is walrus.
        first.conn.execute("BEGIN")
        assert first.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert second.add(doc("narwhal")) is True

        with pytest.raises(sqlite3.OperationalError, match="locked"):
            first.add(doc("dolphin"))
        # Only add()'s own writes rolled back; the caller still owns its txn.
        assert first.conn.in_transaction
        first.conn.rollback()

        assert first.search("dolphin") == []
        assert len(first.search("narwhal")) == 1
        assert first.count() == 1
        _assert_fts_matches_rows(first)


def test_add_joins_an_open_transaction_without_owning_it(tmp_path: Path):
    """DML on idx.conn leaves a transaction open; add() joins it and leaves it open."""
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
        # add() started no transaction of its own, so it must not commit the
        # caller's pending writes either.
        assert idx.conn.in_transaction
        idx.conn.commit()

    with SessionIndex(tmp_path / "s.db") as reopened:
        assert len(reopened.search("walrus")) == 1
        assert reopened.conn.execute("SELECT value FROM index_state").fetchone()[0] == "v"


def test_a_failed_add_does_not_roll_back_the_callers_transaction(tmp_path: Path):
    """A NOT NULL failure inside add() rolls back only add()'s own writes."""
    from gptme_wisdom_mcp.indexer import SessionIndex
    from gptme_wisdom_mcp.parsers import SessionDocument

    good = SessionDocument(
        source="journal",
        path="/journal/session.md",
        date=datetime(2026, 1, 1),
        title="Session",
        content="walrus",
        summary="summary",
    )
    broken = SessionDocument(
        source="journal",
        path="/journal/broken.md",
        date=datetime(2026, 1, 1),
        title=None,  # type: ignore[arg-type]  # NOT NULL violation
        content="narwhal",
        summary="summary",
    )

    with SessionIndex(tmp_path / "s.db") as idx:
        idx.conn.execute("INSERT INTO index_state (key, value) VALUES ('k', 'v')")
        assert idx.add(good) is True
        assert idx.add(broken) is False
        # The caller's row and the successful add() are both still pending.
        assert idx.conn.in_transaction
        idx.conn.commit()

    with SessionIndex(tmp_path / "s.db") as reopened:
        assert reopened.conn.execute("SELECT value FROM index_state").fetchone()[0] == "v"
        assert len(reopened.search("walrus")) == 1
        assert reopened.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        _assert_fts_matches_rows(reopened)


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
