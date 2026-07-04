"""Session indexer using SQLite FTS5 for full-text search.

Zero-dependency solution that provides good-enough text search across all session
types. Can be upgraded to vector search (chromadb) later for semantic matching.

Ranking uses a hybrid score combining FTS5 relevance with temporal decay:
- FTS5 BM25 rank provides keyword relevance
- Exponential decay factor prioritizes recent sessions (half-life: 90 days)
- Source weighting: journal (1.5x) > gptme (1.0x) > claude_code (1.0x)
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

from gptme_wisdom.indexer import BookIndex

from .parsers import (
    SessionDocument,
    iter_claude_code_sessions,
    iter_codex_sessions,
    iter_cursor_sessions,
    parse_codex_session,
    parse_cursor_session,
    iter_gptme_logs,
    iter_journal_entries,
    parse_claude_code_session,
    parse_gptme_log,
    parse_journal_entry,
)

DEFAULT_DB_PATH = Path.home() / ".local/share/bob/session-index.db"

__all__ = ["SessionIndex", "BookIndex", "DEFAULT_DB_PATH"]


class SessionIndex:
    """SQLite FTS5-based session search index."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def _init_schema(self) -> None:
        """Create tables if they don't exist."""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                date TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
                title,
                content,
                summary,
                content='',
                tokenize='porter unicode61'
            );

            CREATE TABLE IF NOT EXISTS index_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def add(self, doc: SessionDocument) -> bool:
        """Add a session document to the index. Returns True if new, False if exists.

        Skips documents from /tmp/worktrees/ paths to avoid indexing duplicates.
        """
        # Safety net: skip worktree copies
        if "/tmp/worktrees/" in doc.path:
            return False

        try:
            cursor = self.conn.execute(
                """
                INSERT INTO sessions (source, path, date, title, summary, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    doc.source,
                    doc.path,
                    doc.date.isoformat(),
                    doc.title,
                    doc.summary,
                    str(doc.metadata),
                ),
            )
            row_id = cursor.lastrowid
            # Add to FTS index
            self.conn.execute(
                """
                INSERT INTO sessions_fts (rowid, title, content, summary)
                VALUES (?, ?, ?, ?)
                """,
                (row_id, doc.title, doc.content, doc.summary),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Already indexed (path is UNIQUE)
            return False

    def search(
        self,
        query: str,
        *,
        source: str | None = None,
        limit: int = 10,
        since: datetime | None = None,
        recency_weight: float = 0.4,
        decay_half_life_days: int = 90,
    ) -> list[dict]:
        """Search indexed sessions using FTS5 with recency-weighted ranking.

        Combines FTS5 relevance (BM25) with temporal decay to prioritize
        recent sessions. The final score blends keyword relevance with recency.

        Args:
            query: Search query (supports FTS5 syntax: AND, OR, NOT, phrases)
            source: Filter by source type ("journal", "gptme", "claude_code")
            limit: Max results to return
            since: Only return sessions after this date
            recency_weight: How much to weight recency vs relevance (0-1).
                0 = pure relevance, 1 = pure recency. Default 0.4.
            decay_half_life_days: Days until recency score halves. Default 90.
        """
        # Fetch more results than needed for re-ranking
        fetch_limit = limit * 3

        # Build query with filters
        conditions = []
        params: list = []

        if source:
            conditions.append("s.source = ?")
            params.append(source)

        if since:
            conditions.append("s.date >= ?")
            params.append(since.isoformat())

        where_clause = ""
        if conditions:
            where_clause = "AND " + " AND ".join(conditions)

        sql = f"""
            SELECT
                s.id, s.source, s.path, s.date, s.title, s.summary, s.metadata,
                rank
            FROM sessions_fts fts
            JOIN sessions s ON s.id = fts.rowid
            WHERE sessions_fts MATCH ?
            {where_clause}
            ORDER BY rank
            LIMIT ?
        """
        params = [query, *params, fetch_limit]

        # Normalize query for FTS5: the unicode61 tokenizer splits on
        # hyphens, so "SWE-bench" is tokenized as "swe" + "bench".
        # Replace hyphens with spaces so the query matches the token stream.
        normalized_query = _normalize_fts_query(query)
        params[0] = normalized_query

        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # FTS query syntax error — try wrapping terms in quotes
            escaped_query = f'"{normalized_query}"'
            params[0] = escaped_query
            rows = self.conn.execute(sql, params).fetchall()

        if not rows:
            return []

        # Re-rank with recency weighting
        now = datetime.now()
        results = []
        for row in rows:
            result = {
                "id": row["id"],
                "source": row["source"],
                "path": row["path"],
                "date": row["date"],
                "title": row["title"],
                "summary": row["summary"],
                "fts_rank": row["rank"],
            }
            result["score"] = _compute_hybrid_score(
                fts_rank=row["rank"],
                date_str=row["date"],
                source=row["source"],
                now=now,
                recency_weight=recency_weight,
                half_life_days=decay_half_life_days,
            )
            results.append(result)

        # Sort by hybrid score (higher is better)
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:limit]

    def purge_paths_matching(self, pattern: str) -> int:
        """Remove entries whose path contains `pattern`. Returns count of deleted rows.

        Uses rebuild-based approach: drops and recreates the FTS table from
        remaining data, which is safer than row-level FTS5 delete operations
        on content-less tables.
        """
        count: int = self.conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE path LIKE ?", (f"%{pattern}%",)
        ).fetchone()[0]
        if count == 0:
            return 0

        # Delete matching rows from sessions table
        self.conn.execute("DELETE FROM sessions WHERE path LIKE ?", (f"%{pattern}%",))

        # Rebuild FTS index from remaining data (safer than row-level deletes
        # on content-less FTS5 tables, which require exact original values)
        self.conn.execute("DROP TABLE IF EXISTS sessions_fts")
        self.conn.execute(
            """
            CREATE VIRTUAL TABLE sessions_fts USING fts5(
                title,
                content,
                summary,
                content='',
                tokenize='porter unicode61'
            )
            """
        )
        # Re-populate FTS from sessions table (title and summary available,
        # content is lost since we don't store it in sessions — use summary)
        self.conn.execute(
            """
            INSERT INTO sessions_fts (rowid, title, content, summary)
            SELECT id, title, summary, summary FROM sessions
            """
        )
        self.conn.commit()
        return count

    def rebuild(self) -> None:
        """Drop all data and recreate schema. Index must be repopulated afterwards."""
        self.conn.executescript(
            """
            DROP TABLE IF EXISTS sessions_fts;
            DROP TABLE IF EXISTS sessions;
            DROP TABLE IF EXISTS index_state;
            """
        )
        self._init_schema()

    def count(self, source: str | None = None) -> int:
        """Count indexed sessions, optionally by source."""
        if source:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE source = ?", (source,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
        return row[0] if row else 0

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _normalize_fts_query(query: str) -> str:
    """Normalize a search query for FTS5's unicode61 tokenizer.

    The tokenizer splits on hyphens and most punctuation, so compound terms
    like "SWE-bench" become two tokens "swe" + "bench". We replace hyphens
    with spaces so the query aligns with the token stream. We also strip
    characters that cause FTS5 syntax errors (unbalanced quotes, etc.).
    """
    import re as _re

    # Replace hyphens with spaces (tokenizer does this too)
    q = query.replace("-", " ")
    # Collapse multiple spaces
    q = _re.sub(r"\s+", " ", q).strip()
    return q


# Source quality weights: journal entries are curated summaries,
# gptme/claude_code are raw conversation logs
_SOURCE_WEIGHTS = {
    "journal": 1.3,
    "gptme": 1.0,
    "claude_code": 1.0,
}


def _compute_hybrid_score(
    fts_rank: float,
    date_str: str,
    source: str,
    now: datetime,
    recency_weight: float = 0.4,
    half_life_days: int = 90,
) -> float:
    """Compute a hybrid relevance score combining FTS5 rank with recency.

    FTS5 rank is negative (more negative = better match), so we normalize it.
    Recency uses exponential decay with configurable half-life.
    Source weighting gives slight preference to curated sources.

    Returns a float where higher = better.
    """
    # Normalize FTS5 rank: rank is negative, closer to 0 = worse match
    # Typical values: -20 (great) to -0.01 (barely matches)
    # Convert to 0-1 scale where 1 = best match
    relevance = min(1.0, -fts_rank / 15.0) if fts_rank < 0 else 0.0

    # Compute recency factor: exponential decay
    try:
        session_date = datetime.fromisoformat(date_str)
    except ValueError:
        session_date = now  # Fallback: treat unparseable dates as recent
    age_days = max(0, (now - session_date).days)
    recency = math.exp(-math.log(2) * age_days / half_life_days)

    # Source quality weight
    source_weight = _SOURCE_WEIGHTS.get(source, 1.0)

    # Blend relevance and recency
    score = ((1.0 - recency_weight) * relevance + recency_weight * recency) * source_weight
    return score


def index_all_sessions(
    index: SessionIndex,
    *,
    journal_dir: Path | None = None,
    gptme_logs_dir: Path | None = None,
    cc_projects_dir: Path | None = None,
    cursor_home_dir: Path | None = None,
    codex_home_dir: Path | None = None,
    verbose: bool = False,
) -> dict[str, int]:
    """Index all available session sources.

    Returns counts of new sessions indexed per source.
    Pass cursor_home_dir or codex_home_dir to enable cross-agent indexing.
    """
    counts: dict[str, int] = {
        "journal": 0,
        "gptme": 0,
        "claude_code": 0,
        "cursor": 0,
        "codex": 0,
    }

    # Index journal entries
    if journal_dir and journal_dir.exists():
        for path in iter_journal_entries(journal_dir):
            if _parse_and_add(index, path, parse_journal_entry, verbose=verbose):
                counts["journal"] += 1
                if verbose:
                    print(f"  + journal: {path}")

    # Index gptme logs
    if gptme_logs_dir and gptme_logs_dir.exists():
        for path in _iter_with_limit(iter_gptme_logs(gptme_logs_dir)):
            if _parse_and_add(index, path, parse_gptme_log, verbose=verbose):
                counts["gptme"] += 1
                if verbose and counts["gptme"] % 100 == 0:
                    print(f"  + gptme: {counts['gptme']} indexed...")

    # Index Claude Code sessions
    if cc_projects_dir and cc_projects_dir.exists():
        for path in iter_claude_code_sessions(cc_projects_dir):
            if _parse_and_add(index, path, parse_claude_code_session, verbose=verbose):
                counts["claude_code"] += 1
                if verbose:
                    print(f"  + claude_code: {path}")

    # Index Cursor sessions (opt-in)
    if cursor_home_dir is not None:
        for path in iter_cursor_sessions(cursor_home_dir):
            if _parse_and_add(index, path, parse_cursor_session, verbose=verbose):
                counts["cursor"] += 1
                if verbose:
                    print(f"  + cursor: {path}")

    # Index Codex sessions (opt-in)
    if codex_home_dir is not None:
        for path in iter_codex_sessions(codex_home_dir):
            if _parse_and_add(index, path, parse_codex_session, verbose=verbose):
                counts["codex"] += 1
                if verbose:
                    print(f"  + codex: {path}")

    return counts


def _iter_with_limit(it: Iterator, max_items: int = 50000) -> Iterator:
    """Safety limit for iterators to prevent runaway indexing."""
    for i, item in enumerate(it):
        if i >= max_items:
            break
        yield item


def _parse_and_add(
    index: SessionIndex,
    path: Path,
    parser: Callable[[Path], SessionDocument | None],
    *,
    verbose: bool = False,
) -> bool:
    """Parse one session path and add it to the index, skipping bad files."""
    try:
        doc = parser(path)
    except Exception as e:
        if verbose:
            print(f"  ! skipped {path}: {type(e).__name__}: {e}")
        return False
    return bool(doc and index.add(doc))
