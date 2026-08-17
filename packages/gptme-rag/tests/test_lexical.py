"""Tests for the TF-IDF lexical retrieval backend."""

from pathlib import Path

import pytest

from gptme_rag.indexing.document import Document
from gptme_rag.lexical import TfidfIndex

sklearn = pytest.importorskip("sklearn")


def _doc(text: str, source: str = "doc.md") -> Document:
    return Document(content=text, metadata={"source": source}, source_path=Path(source))


def test_index_and_search_ranks_identifier_exactly(tmp_path: Path):
    idx = TfidfIndex()
    docs = [
        _doc("The quick brown fox jumps over the lazy dog", "a.md"),
        _doc("def retry_backoff(max_attempts: int) -> None:", "b.md"),
        _doc("Configures the database connection pool timeout", "c.md"),
    ]
    idx.index(docs)

    # A rare identifier term should surface the matching doc first.
    hits = idx.search("retry_backoff max_attempts", n_results=1)
    assert len(hits) == 1
    assert hits[0].document.metadata["source"] == "b.md"


def test_relevance_floor_filters_weak_hits(tmp_path: Path):
    idx = TfidfIndex(relevance_floor=0.2)
    docs = [
        _doc("alpha beta gamma delta", "a.md"),
        _doc("completely unrelated subject matter here", "b.md"),
    ]
    idx.index(docs)
    hits = idx.search("alpha", n_results=5)
    # The matching doc (~0.378 similarity) clears a 0.2 floor; the unrelated
    # doc scores 0.0 and is dropped.
    assert [h.document.metadata["source"] for h in hits] == ["a.md"]
    assert all(h.score >= 0.2 for h in hits)


def test_exclude_paths_skips_documents(tmp_path: Path):
    idx = TfidfIndex()
    docs = [
        _doc("retry_backoff implementation", "a.md"),
        _doc("retry_backoff implementation", "b.md"),
    ]
    idx.index(docs)
    hits = idx.search("retry_backoff", n_results=5, exclude_paths={"a.md"})
    assert all(h.document.metadata["source"] != "a.md" for h in hits)


def test_save_and_load_roundtrip(tmp_path: Path):
    idx = TfidfIndex()
    idx.index([_doc("retry_backoff function", "a.md"), _doc("other content", "b.md")])
    path = tmp_path / "index.pkl"
    idx.save(path)
    assert path.exists()

    loaded = TfidfIndex.load(path)
    hits = loaded.search("retry_backoff", n_results=1)
    assert hits[0].document.metadata["source"] == "a.md"


def test_unindexed_search_returns_empty():
    idx = TfidfIndex()
    assert idx.search("anything") == []


def test_index_empty_documents_does_not_raise():
    """index([]) must not raise ValueError from sklearn; search() must return []."""
    idx = TfidfIndex()
    idx.index([])  # was: ValueError("empty vocabulary") from TfidfVectorizer
    assert idx.search("anything") == []
