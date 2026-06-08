"""Local-first semantic search over gptme-codegraph symbols.

Provides dependency-free lexical search (BM25-like) on symbol documents
extracted from a ``SymbolIndex``. Follow-up phases will add an optional
local embedding backend.

Usage:
    from gptme_codegraph.search import (
        SearchDocument,
        SearchResult,
        extract_search_documents,
        LexicalScorer,
        SearchBackend,
    )

    index = build_index(...)
    docs = extract_search_documents(index, root)
    scorer = LexicalScorer()
    scorer.index(docs)
    results = scorer.search("retry logic", limit=5)
"""

import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from gptme_codegraph.core import SymbolIndex

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class SearchDocument:
    """A searchable document derived from a single symbol."""

    qualified_id: str
    kind: str
    name: str
    file: str
    start_line: int
    end_line: int
    parent_class: str | None = None
    docstring: str = ""
    calls: list[str] = field(default_factory=list)
    snippet: str = ""  # first N non-empty source lines, capped


@dataclass
class SearchResult:
    """A single ranked search result."""

    score: float
    qualified_id: str
    name: str
    kind: str
    file: str
    start_line: int
    end_line: int
    parent_class: str | None = None
    why: str = ""


# ---------------------------------------------------------------------------
# Document extraction
# ---------------------------------------------------------------------------

_SNIPPET_MAX_LINES = 5
_SNIPPET_MAX_CHARS = 500
_DOCSTRING_MAX_CHARS = 500


def _extract_snippet(symbol_lines: list[str]) -> str:
    """Extract first N non-empty lines from a symbol body, capped."""
    non_empty = [l for l in symbol_lines if l.strip()]
    snippet_lines = non_empty[:_SNIPPET_MAX_LINES]
    snippet = "\n".join(snippet_lines).strip()
    if len(snippet) > _SNIPPET_MAX_CHARS:
        snippet = snippet[:_SNIPPET_MAX_CHARS] + "..."
    return snippet


def _read_symbol_body(filepath: Path, start_line: int, end_line: int) -> list[str]:
    """Read source lines for a symbol from file."""
    try:
        with open(filepath) as f:
            lines = f.readlines()
        # start_line and end_line are 1-based, inclusive
        return lines[start_line - 1 : end_line]
    except (OSError, IndexError):
        return []


def extract_search_documents(index: SymbolIndex, root: Path) -> list[SearchDocument]:
    """Build one search document per indexed symbol.

    Reads source files to extract docstrings and short snippets for each
    symbol definition.
    """
    docs: list[SearchDocument] = []
    seen: set[str] = set()  # deduplicate by qualified_id

    for name, entries in index.entries.items():
        for entry in entries:
            qid = entry.qualified_id()
            if qid in seen:
                continue
            seen.add(qid)

            docstring = ""
            snippet = ""
            filepath = (
                root / entry.file
                if not Path(entry.file).is_absolute()
                else Path(entry.file)
            )

            if filepath.exists():
                body_lines = _read_symbol_body(
                    filepath, entry.start_line, entry.end_line
                )
                snippet = _extract_snippet(body_lines)

                # Extract docstring from the symbol body
                docstring = _extract_docstring(body_lines)

            doc = SearchDocument(
                qualified_id=qid,
                kind=entry.kind,
                name=entry.name,
                file=entry.file,
                start_line=entry.start_line,
                end_line=entry.end_line,
                parent_class=entry.parent_class,
                docstring=docstring[:_DOCSTRING_MAX_CHARS],
                snippet=snippet,
                calls=[],
            )
            docs.append(doc)

    return docs


def _extract_docstring(body_lines: list[str]) -> str:
    """Extract a docstring from symbol body lines.

    Handles both triple-quoted (''' and \"\"\") and single-line docstrings.
    """
    text = "".join(body_lines).strip()
    if not text:
        return ""

    # Try triple double quotes
    m = re.search(r'"""(.*?)"""', text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # Try triple single quotes
    m = re.search(r"'''(.*?)'''", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # Try single-line docstring (after def/class line)
    lines = text.split("\n")
    if len(lines) >= 2:
        second_line = lines[1].strip()
        if second_line.startswith(('"""', "'''")) and second_line.endswith(
            ('"""', "'''")
        ):
            return second_line.strip("\"'").strip()

    return ""


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase words.

    Splits on non-alphanumeric characters and handles camelCase and
    snake_case by splitting on case transitions and underscores.
    """
    # First, split camelCase and PascalCase
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    # Replace underscores and non-alphanumeric with spaces
    text = re.sub(r"[^a-zA-Z0-9]", " ", text)
    return [t.lower() for t in text.split() if len(t) > 1 or t.isalnum()]


# ---------------------------------------------------------------------------
# BM25 scorer
# ---------------------------------------------------------------------------


class LexicalScorer:
    """Dependency-free BM25-like lexical scorer.

    Uses BM25-Okapi weighting with standard parameters (k1=1.5, b=0.75).
    """

    k1: float = 1.5
    b: float = 0.75

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._documents: list[SearchDocument] = []
        self._doc_freqs: dict[str, int] = {}
        self._doc_lengths: list[int] = []
        self._avg_doc_length: float = 0.0
        self._num_docs: int = 0
        self._idf_cache: dict[str, float] = {}
        self._ready = False

    def index(self, documents: list[SearchDocument]) -> None:
        """Index a list of search documents."""
        self._documents = documents
        self._num_docs = len(documents)
        self._doc_freqs = Counter()
        self._doc_lengths = []

        for doc in documents:
            field_text = " ".join(
                [
                    doc.name,
                    doc.kind,
                    doc.parent_class or "",
                    doc.docstring,
                    doc.snippet,
                    doc.qualified_id,
                ]
            )
            tokens = _tokenize(field_text)
            unique_terms = set(tokens)
            for term in unique_terms:
                self._doc_freqs[term] += 1
            self._doc_lengths.append(len(tokens))

        self._avg_doc_length = (
            sum(self._doc_lengths) / self._num_docs if self._num_docs > 0 else 0.0
        )
        self._idf_cache = {}
        self._ready = True

    def _idf(self, term: str) -> float:
        """Compute IDF for a term."""
        if term not in self._idf_cache:
            df = self._doc_freqs.get(term, 0)
            if df == 0:
                return 0.0
            self._idf_cache[term] = math.log(
                (self._num_docs - df + 0.5) / (df + 0.5) + 1.0
            )
        return self._idf_cache[term]

    def _score_document(self, query_tokens: list[str], doc_idx: int) -> float:
        """Score a single document against query tokens."""
        doc = self._documents[doc_idx]
        field_text = " ".join(
            [
                doc.name,
                doc.kind,
                doc.parent_class or "",
                doc.docstring,
                doc.snippet,
                doc.qualified_id,
            ]
        )
        doc_tokens = _tokenize(field_text)
        doc_len = len(doc_tokens)
        term_freqs = Counter(doc_tokens)

        score = 0.0
        for term in query_tokens:
            tf = term_freqs.get(term, 0)
            if tf == 0:
                continue
            idf = self._idf(term)
            numerator = tf * (self.k1 + 1)
            denominator = tf + self.k1 * (
                1 - self.b + self.b * (doc_len / self._avg_doc_length)
            )
            score += idf * (numerator / denominator)

        return score

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        """Search indexed documents and return ranked results."""
        if not self._ready or not self._documents:
            return []

        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        scored: list[tuple[float, int]] = []
        for i in range(self._num_docs):
            s = self._score_document(query_tokens, i)
            if s > 0:
                scored.append((s, i))

        # Sort by score descending
        scored.sort(key=lambda x: -x[0])

        results: list[SearchResult] = []
        for score, idx in scored[:limit]:
            doc = self._documents[idx]
            why_parts = []
            for term in query_tokens:
                if doc.name and term in _tokenize(doc.name):
                    why_parts.append("name")
                if doc.docstring and term in _tokenize(doc.docstring):
                    why_parts.append("docstring")
                if doc.snippet and term in _tokenize(doc.snippet):
                    why_parts.append("body")
                if doc.qualified_id and term in _tokenize(doc.qualified_id):
                    why_parts.append("qualified_id")

            results.append(
                SearchResult(
                    score=round(score, 4),
                    qualified_id=doc.qualified_id,
                    name=doc.name,
                    kind=doc.kind,
                    file=doc.file,
                    start_line=doc.start_line,
                    end_line=doc.end_line,
                    parent_class=doc.parent_class,
                    why="/".join(dict.fromkeys(why_parts)) if why_parts else "matched",
                )
            )

        return results


# ---------------------------------------------------------------------------
# Search backend protocol + registry
# ---------------------------------------------------------------------------


class SearchBackend(Protocol):
    """Protocol for search backends.

    Both the lexical and (future) semantic backends implement this.
    """

    name: str

    def index(self, documents: list[SearchDocument]) -> None: ...

    def search(self, query: str, limit: int) -> list[SearchResult]: ...


# ---------------------------------------------------------------------------
# SQLite search-document cache
# ---------------------------------------------------------------------------


def _ensure_search_tables(conn: sqlite3.Connection) -> None:
    """Create search-document cache tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS search_documents (
            directory TEXT NOT NULL,
            qualified_id TEXT NOT NULL,
            file TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            document TEXT NOT NULL,
            PRIMARY KEY (directory, qualified_id)
        );
    """)


def save_search_documents(
    conn: sqlite3.Connection, directory: str, docs: list[SearchDocument]
) -> None:
    """Persist search documents to SQLite cache."""
    import hashlib
    import json

    _ensure_search_tables(conn)
    conn.execute("DELETE FROM search_documents WHERE directory = ?", (directory,))

    for doc in docs:
        content_hash = hashlib.sha256(
            json.dumps(
                {
                    "name": doc.name,
                    "kind": doc.kind,
                    "file": doc.file,
                    "start_line": doc.start_line,
                    "end_line": doc.end_line,
                    "parent_class": doc.parent_class,
                    "docstring": doc.docstring,
                    "snippet": doc.snippet,
                    "calls": doc.calls,
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()[:16]

        doc_json = json.dumps(
            {
                "qualified_id": doc.qualified_id,
                "name": doc.name,
                "kind": doc.kind,
                "file": doc.file,
                "start_line": doc.start_line,
                "end_line": doc.end_line,
                "parent_class": doc.parent_class,
                "docstring": doc.docstring,
                "snippet": doc.snippet,
                "calls": doc.calls,
            },
            default=str,
        )

        conn.execute(
            """INSERT INTO search_documents (directory, qualified_id, file, start_line, end_line, content_hash, document)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                directory,
                doc.qualified_id,
                doc.file,
                doc.start_line,
                doc.end_line,
                content_hash,
                doc_json,
            ),
        )
    conn.commit()


def load_search_documents(
    conn: sqlite3.Connection, directory: str
) -> list[SearchDocument]:
    """Load search documents from SQLite cache."""
    import json

    _ensure_search_tables(conn)
    rows = conn.execute(
        "SELECT document FROM search_documents WHERE directory = ?",
        (directory,),
    ).fetchall()

    docs: list[SearchDocument] = []
    for (doc_json,) in rows:
        data = json.loads(doc_json)
        docs.append(
            SearchDocument(
                qualified_id=data["qualified_id"],
                kind=data["kind"],
                name=data["name"],
                file=data["file"],
                start_line=data["start_line"],
                end_line=data["end_line"],
                parent_class=data.get("parent_class"),
                docstring=data.get("docstring", ""),
                snippet=data.get("snippet", ""),
                calls=data.get("calls", []),
            )
        )
    return docs
