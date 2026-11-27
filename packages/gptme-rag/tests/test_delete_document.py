"""Indexer.delete_document: delete the exact document and report a miss honestly.

Regression tests for gptme/gptme-contrib#2561.
"""

import pytest

pytest.importorskip("chromadb")

from gptme_rag.indexing import indexer as indexer_module  # noqa: E402
from gptme_rag.indexing.document import Document  # noqa: E402


def _words(n: int) -> str:
    return " ".join(f"w{i}" for i in range(n))


def _index_two_files(indexer, tmp_path) -> tuple[str, set[str], object]:
    """Index a kept file and a target file; return (target base id, kept ids, kept path)."""
    keep = tmp_path / "keep.txt"
    keep.write_text(_words(120))
    indexer.index_file(keep)
    keep_ids = set(indexer.collection.get()["ids"])

    target = tmp_path / "big.txt"
    target.write_text(_words(200))
    assert indexer.index_file(target) > 1, "fixture must produce several chunks"
    target_ids = set(indexer.collection.get()["ids"]) - keep_ids
    base_ids = {i.split("#chunk")[0] for i in target_ids}
    assert len(base_ids) == 1
    return base_ids.pop(), keep_ids, keep


def test_delete_document_by_base_id_deletes_chunks(indexer, tmp_path):
    """The base id is not any chunk's id; its chunks must still be deleted."""
    base_id, keep_ids, _ = _index_two_files(indexer, tmp_path)

    assert indexer.delete_document(base_id) is True
    assert set(indexer.collection.get()["ids"]) == keep_ids


def test_delete_document_by_source_path(indexer, tmp_path):
    """The existing source-path form keeps working."""
    _, keep_ids, keep = _index_two_files(indexer, tmp_path)
    remaining = set(indexer.collection.get()["ids"]) - keep_ids

    assert indexer.delete_document(str(keep.resolve())) is True
    assert set(indexer.collection.get()["ids"]) == remaining


def test_delete_document_unknown_id_returns_false(indexer, tmp_path):
    """Deleting an id that matches nothing reports False and changes nothing."""
    _index_two_files(indexer, tmp_path)
    before = set(indexer.collection.get()["ids"])

    assert indexer.delete_document("/no/such/file@deadbeef") is False
    assert set(indexer.collection.get()["ids"]) == before


def test_delete_document_exact_id_does_not_claim_id_prefix(indexer):
    """Only the exact id and its "#chunk" ids go, not ids that merely extend it."""
    indexer.add_document(Document(content="first doc", metadata={"source": "a"}, doc_id="doc"))
    indexer.add_document(Document(content="second doc", metadata={"source": "b"}, doc_id="doc2"))

    assert indexer.delete_document("doc") is True
    assert indexer.collection.get()["ids"] == ["doc2"]


def test_delete_document_reads_and_deletes_in_pages(indexer, tmp_path, monkeypatch):
    """No unbounded get()/delete(), which can exceed SQLite's variable limit."""
    base_id, keep_ids, _ = _index_two_files(indexer, tmp_path)
    monkeypatch.setattr(indexer_module, "_PAGE_SIZE", 3)
    real_get, real_delete = indexer.collection.get, indexer.collection.delete

    def bounded_get(*args, **kwargs):
        assert kwargs.get("limit") is not None and kwargs["limit"] <= 3, f"unbounded get: {kwargs}"
        return real_get(*args, **kwargs)

    def bounded_delete(*args, **kwargs):
        assert len(kwargs["ids"]) <= 3, f"unbounded delete: {kwargs}"
        return real_delete(*args, **kwargs)

    monkeypatch.setattr(indexer.collection, "get", bounded_get)
    monkeypatch.setattr(indexer.collection, "delete", bounded_delete)

    assert indexer.delete_document(base_id) is True
    assert set(real_get()["ids"]) == keep_ids


def test_delete_document_retries_on_stale_handle(indexer, tmp_path, monkeypatch):
    """A NotFoundError mid-delete re-binds the handle and retries once."""
    from chromadb.errors import NotFoundError

    base_id, keep_ids, _ = _index_two_files(indexer, tmp_path)
    real_delete = indexer.collection.delete
    calls: list[int] = []

    def delete_raising_once(*args, **kwargs):
        if not calls:
            calls.append(1)
            raise NotFoundError("stale collection handle")
        return real_delete(*args, **kwargs)

    monkeypatch.setattr(indexer.collection, "delete", delete_raising_once)

    assert indexer.delete_document(base_id) is True
    assert calls, "the delete never hit the stale handle"
    assert set(indexer.collection.get()["ids"]) == keep_ids


def _spy_refresh(indexer, monkeypatch) -> list[int]:
    calls: list[int] = []
    real_refresh = indexer._refresh_collection

    def refresh():
        calls.append(1)
        real_refresh()

    monkeypatch.setattr(indexer, "_refresh_collection", refresh)
    return calls


def _concurrent_indexer(indexer):
    from gptme_rag.indexing.indexer import Indexer

    return Indexer(
        persist_directory=indexer.persist_directory,
        chunk_size=indexer.processor.chunk_size,
        chunk_overlap=indexer.processor.chunk_overlap,
        enable_persist=True,
        collection_name=indexer.collection_name,
    )


def test_delete_document_after_concurrent_reset_and_reindex(indexer, tmp_path, monkeypatch):
    """A concurrent reset invalidates the handle; the re-indexed copy is still deleted."""
    base_id, keep_ids, keep = _index_two_files(indexer, tmp_path)
    other = _concurrent_indexer(indexer)
    other.reset_collection()
    other.index_file(tmp_path / "big.txt")
    other.index_file(keep)
    refreshed = _spy_refresh(indexer, monkeypatch)

    assert indexer.delete_document(base_id) is True
    assert refreshed, "the reset never invalidated the cached handle"
    assert set(other.collection.get()["ids"]) == keep_ids


def test_delete_document_after_concurrent_reset_reports_miss(indexer, tmp_path, monkeypatch):
    """A concurrent reset already removed the document: report a miss, not success."""
    base_id, _, _ = _index_two_files(indexer, tmp_path)
    other = _concurrent_indexer(indexer)
    other.reset_collection()
    refreshed = _spy_refresh(indexer, monkeypatch)

    assert indexer.delete_document(base_id) is False
    assert refreshed, "the reset never invalidated the cached handle"
    assert other.collection.get()["ids"] == []
