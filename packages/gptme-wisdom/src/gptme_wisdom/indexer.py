"""SQLite FTS5 BM25 index for the reference-book wisdom layer."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Iterator

from .parsers import BookDocument

__all__ = ["BookIndex", "DEFAULT_DB_PATH"]

DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "gptme" / "wisdom.db"


def _normalize_fts_query(query: str) -> str:
    """Escape FTS5 special characters for safe querying.

    Removes FTS5 operator tokens that would cause syntax errors in a
    bare MATCH query. The search fallback wraps the stripped query in
    double quotes for a literal phrase match if the unquoted version
    still fails.
    """
    result = query.replace('"', '""')
    for ch in ("(", ")", "*", "^", "+", "-", "~"):
        result = result.replace(ch, "")
    return result


class BookIndex:
    """SQLite FTS5-based index for the reference-book wisdom layer.

    Uses a separate DB from session indexes so book relevance never
    contaminates session-memory scores. Ranking is pure BM25 — books are
    timeless, so recency decay does not apply.

    Can be used as a context manager (``with BookIndex() as idx:``).
    """

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def __enter__(self) -> "BookIndex":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                chapter TEXT NOT NULL DEFAULT '',
                section TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                url TEXT NOT NULL DEFAULT '',
                license TEXT NOT NULL DEFAULT 'unknown',
                page INTEGER,
                content_hash TEXT NOT NULL UNIQUE
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
                title,
                chapter,
                section,
                content,
                content='',
                tokenize='porter unicode61'
            );
            """
        )
        self.conn.commit()

    def add(self, doc: BookDocument, *, commit: bool = True) -> bool:
        """Add one book chunk. Returns True if new, False if duplicate.

        Args:
            doc: The book document chunk to index.
            commit: If True, commit after insert. Set False for batch
                    insertion via add_many() to avoid per-chunk commits.
        """
        content_hash = hashlib.sha256(f"{doc.source}\x00{doc.content}".encode("utf-8")).hexdigest()
        try:
            cursor = self.conn.execute(
                """
                INSERT INTO books
                    (source, title, chapter, section, content, url, license, page,
                     content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc.source,
                    doc.title,
                    doc.chapter,
                    doc.section,
                    doc.content,
                    doc.url,
                    doc.license,
                    doc.page,
                    content_hash,
                ),
            )
            row_id = cursor.lastrowid
            self.conn.execute(
                """
                INSERT INTO books_fts (rowid, title, chapter, section, content)
                VALUES (?, ?, ?, ?, ?)
                """,
                (row_id, doc.title, doc.chapter, doc.section, doc.content),
            )
            if commit:
                self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def add_many(self, docs: Iterator[BookDocument]) -> int:
        """Add many book chunks. Returns count of newly-inserted chunks.

        Batches all inserts into a single commit for performance —
        avoids per-chunk WAL sync overhead for bulk ingestion.
        """
        count = 0
        for doc in docs:
            if self.add(doc, commit=False):
                count += 1
        self.conn.commit()
        return count

    def search(
        self,
        query: str,
        *,
        source: str | None = None,
        limit: int = 5,
    ) -> list[dict]:
        """Search book chunks via FTS5 BM25. Returns dicts ordered by relevance."""
        conditions = []
        params: list = []
        if source:
            conditions.append("b.source = ?")
            params.append(source)
        where_clause = ("AND " + " AND ".join(conditions)) if conditions else ""

        sql = f"""
            SELECT b.id, b.source, b.title, b.chapter, b.section, b.content,
                   b.url, b.license, b.page, rank
            FROM books_fts fts
            JOIN books b ON b.id = fts.rowid
            WHERE books_fts MATCH ?
            {where_clause}
            ORDER BY rank
            LIMIT ?
        """
        normalized = _normalize_fts_query(query)
        # First attempt: bare MATCH (supports FTS5 syntax if query is clean)
        # Fallback: wrap in double quotes for literal phrase match
        for args in ([normalized, *params, limit], [f'"{normalized}"', *params, limit]):
            try:
                rows = self.conn.execute(sql, args).fetchall()
                break
            except sqlite3.OperationalError:
                continue
        else:
            rows = []

        results = []
        for row in rows:
            fts_rank = row["rank"]
            relevance = min(1.0, -fts_rank / 15.0) if fts_rank < 0 else 0.0
            results.append(
                {
                    "id": row["id"],
                    "source": row["source"],
                    "title": row["title"],
                    "chapter": row["chapter"],
                    "section": row["section"],
                    "content": row["content"],
                    "url": row["url"],
                    "license": row["license"],
                    "page": row["page"],
                    "score": relevance,
                }
            )
        return results

    def count(self, source: str | None = None) -> int:
        """Return total chunk count, optionally filtered by source slug."""
        if source:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM books WHERE source = ?", (source,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) FROM books").fetchone()
        return row[0] if row else 0

    def sources(self) -> list[dict]:
        """Return indexed sources with chunk counts."""
        rows = self.conn.execute(
            """
            SELECT source, title, url, license, COUNT(*) as chunks
            FROM books
            GROUP BY source
            ORDER BY source
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def remove_source(self, source: str) -> int:
        """Remove all chunks for a source slug. Returns count removed."""
        rows = self.conn.execute(
            "SELECT id, title, chapter, section, content FROM books WHERE source = ?",
            (source,),
        ).fetchall()
        if not rows:
            return 0
        # FTS5 contentless tables require the special 'delete' command (can't DELETE directly).
        for row in rows:
            self.conn.execute(
                "INSERT INTO books_fts(books_fts, rowid, title, chapter, section, content) "
                "VALUES ('delete', ?, ?, ?, ?, ?)",
                (row["id"], row["title"], row["chapter"], row["section"], row["content"]),
            )
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        self.conn.execute(f"DELETE FROM books WHERE id IN ({placeholders})", ids)
        self.conn.commit()
        return len(rows)
