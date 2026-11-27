"""Indexer lifecycle: emptied files and add-before-sweep re-indexing.

Regression tests for gptme/gptme-contrib#2560 (R1 of #2356): an emptied file
must drop its indexed chunks without storing the empty-file tombstone, and a
failed replacement write must leave the previous chunks in place.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

pytest.importorskip("chromadb")

from gptme_rag.indexing import indexer as indexer_module  # noqa: E402
from gptme_rag.indexing.document import Document  # noqa: E402


def _words(n: int) -> str:
    return " ".join(f"w{i}" for i in range(n))


def _ids_for(indexer, path) -> list[str]:
    return indexer.collection.get(where={"source": str(path.resolve())})["ids"]


def test_index_directory_drops_emptied_file(indexer, tmp_path):
    """An emptied file disappears from the index instead of becoming an empty doc."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    f = corpus / "note.md"
    f.write_text("hello world, some real content")
    indexer.index_directory(corpus, glob_pattern="**/*.md")
    assert _ids_for(indexer, f)
    f.write_text("")

    assert indexer.index_directory(corpus, glob_pattern="**/*.md") == 1

    got = indexer.collection.get(where={"source": str(f.resolve())})
    assert got["ids"] == [], f"tombstone stored as a document: {got['documents']!r}"


def test_index_directory_emptied_file_keeps_other_files(indexer, tmp_path):
    """Sweeping an emptied file's chunks leaves the other files' chunks alone."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    emptied = corpus / "emptied.md"
    kept = corpus / "kept.md"
    emptied.write_text(_words(120))
    kept.write_text(_words(120) + " kept")
    indexer.index_directory(corpus, glob_pattern="**/*.md")
    kept_ids = _ids_for(indexer, kept)
    assert kept_ids
    emptied.write_text("\n")

    indexer.index_directory(corpus, glob_pattern="**/*.md")

    assert _ids_for(indexer, emptied) == []
    assert sorted(_ids_for(indexer, kept)) == sorted(kept_ids)


def test_index_file_drops_emptied_file(indexer, tmp_path):
    """index_file on an emptied file removes its chunks and stores nothing."""
    f = tmp_path / "note.md"
    f.write_text(_words(200))
    assert indexer.index_file(f) > 1
    f.write_text("   \n")

    assert indexer.index_file(f) == 0

    got = indexer.collection.get(where={"source": str(f.resolve())})
    assert got["ids"] == [], f"left behind: {got['documents']!r}"


def test_index_file_never_indexed_empty_file_stores_nothing(indexer, tmp_path):
    """A brand-new empty file is a no-op, not an empty stored document."""
    f = tmp_path / "empty.md"
    f.write_text("")

    assert indexer.index_file(f) == 0
    assert indexer.collection.get()["ids"] == []


def test_index_file_replaces_stale_chunks(indexer, tmp_path):
    """Re-indexing changed content drops the old generation's chunks."""
    f = tmp_path / "note.md"
    f.write_text(_words(200))
    indexer.index_file(f)
    old_ids = set(_ids_for(indexer, f))

    f.write_text(_words(200) + " rewritten tail words")
    n_new = indexer.index_file(f)

    remaining = set(_ids_for(indexer, f))
    assert not (old_ids & remaining), "stale chunks of the old content survived"
    assert len(remaining) == n_new


def test_index_file_unchanged_reindex_is_idempotent(indexer, tmp_path):
    """Re-indexing unchanged content keeps exactly the same ids (no error, no loss)."""
    f = tmp_path / "note.md"
    f.write_text(_words(200))
    indexer.index_file(f)
    before = sorted(_ids_for(indexer, f))

    indexer.index_file(f)

    assert sorted(_ids_for(indexer, f)) == before


def test_index_file_keeps_old_chunks_when_add_fails(indexer, tmp_path):
    """A failed add must not leave the file with its old chunks deleted.

    A tombstone alongside real chunks for one source cannot come from a plain
    empty file, but a collector that trims trailing bytes can produce it. The
    delete must therefore never run before a successful add.
    """
    f = tmp_path / "note.md"
    f.write_text(_words(200))
    indexer.index_file(f)
    source = str(f.resolve())
    before = indexer.collection.get(where={"source": source})["ids"]
    assert before

    tombstone = Document(content="", metadata={"source": source})
    replacement = Document(content=_words(200) + " changed", metadata={"source": source})
    indexer.collect_documents = lambda path: [tombstone, replacement]  # type: ignore[method-assign]

    def failing_add(documents):
        raise RuntimeError("embedding backend unavailable")

    indexer.add_documents = failing_add  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        indexer.index_file(f)

    assert indexer.collection.get(where={"source": source})["ids"] == before


def test_index_directory_keeps_old_chunks_when_add_fails(indexer, tmp_path):
    """index_directory must not delete before the add succeeds."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    f = corpus / "note.md"
    f.write_text(_words(200))
    indexer.index_directory(corpus, glob_pattern="**/*.md")
    source = str(f.resolve())
    before = indexer.collection.get(where={"source": source})["ids"]
    assert before

    f.write_text(_words(200) + " changed")

    def failing_add(documents, batch_size=10):
        raise RuntimeError("embedding backend unavailable")

    indexer.add_documents = failing_add  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        indexer.index_directory(corpus, glob_pattern="**/*.md")

    assert indexer.collection.get(where={"source": source})["ids"] == before


def test_stale_sweep_scan_holds_the_writer_lock(indexer, tmp_path):
    """The pre-add id snapshot is read under the writer lock.

    The scan is offset-based, so a concurrent write between pages would skip an
    id and leave that chunk behind as a stale duplicate.
    """
    from gptme_rag.indexing.gc import index_writer_lock

    f = tmp_path / "note.md"
    f.write_text(_words(200))
    indexer.index_file(f)
    f.write_text(_words(200) + " changed")

    real_get = indexer.collection.get
    probes: list[bool] = []

    def get_while_probing_lock(*args, **kwargs):
        if kwargs.get("where") and not kwargs.get("ids"):
            try:
                with index_writer_lock(indexer.persist_directory, blocking=False):
                    probes.append(False)
            except RuntimeError:
                probes.append(True)
        return real_get(*args, **kwargs)

    indexer.collection.get = get_while_probing_lock  # type: ignore[method-assign]
    try:
        indexer.index_file(f)
    finally:
        indexer.collection.get = real_get  # type: ignore[method-assign]

    assert probes and all(probes), "the stale-id scan read ids without the writer lock"


def test_stale_sweep_reads_and_deletes_in_pages(indexer, tmp_path, monkeypatch):
    """No unbounded get()/delete() in the sweep: SQLite's variable limit applies."""
    f = tmp_path / "note.md"
    f.write_text(_words(200))
    n_chunks = indexer.index_file(f)
    assert n_chunks > 3
    f.write_text("")

    monkeypatch.setattr(indexer_module, "_PAGE_SIZE", 3)
    real_get, real_delete = indexer.collection.get, indexer.collection.delete

    def bounded_get(*args, **kwargs):
        assert kwargs.get("limit") is not None and kwargs["limit"] <= 3, f"unbounded: {kwargs}"
        return real_get(*args, **kwargs)

    def bounded_delete(*args, **kwargs):
        assert len(kwargs["ids"]) <= 3, f"unbounded delete: {kwargs}"
        return real_delete(*args, **kwargs)

    monkeypatch.setattr(indexer.collection, "get", bounded_get)
    monkeypatch.setattr(indexer.collection, "delete", bounded_delete)

    assert indexer.index_file(f) == 0
    assert real_get(where={"source": str(f.resolve())})["ids"] == []


def test_sweep_refreshes_a_stale_collection_handle(indexer, tmp_path, monkeypatch):
    """A NotFoundError on the sweep delete re-binds the handle and retries once."""
    from chromadb.errors import NotFoundError

    f = tmp_path / "note.md"
    f.write_text(_words(200))
    indexer.index_file(f)
    stale_ids = set(indexer.collection.get()["ids"])
    f.write_text(_words(200) + " changed")

    real_delete = indexer.collection.delete
    calls: list[int] = []

    def delete_raising_once(*args, **kwargs):
        if not calls:
            calls.append(1)
            raise NotFoundError("stale collection handle")
        return real_delete(*args, **kwargs)

    monkeypatch.setattr(indexer.collection, "delete", delete_raising_once)
    indexer.index_file(f)

    assert calls, "the sweep never hit the stale handle"
    remaining = set(indexer.collection.get()["ids"])
    assert not (stale_ids & remaining), f"stale chunks survived the retried sweep: {remaining}"


def test_snapshot_refreshes_a_stale_collection_handle(indexer, tmp_path, monkeypatch):
    """A reset by another indexer must not make the next directory reindex fail."""
    from gptme_rag.indexing.indexer import Indexer

    f = tmp_path / "note.md"
    f.write_text(_words(200))
    indexer.index_file(f)
    other = Indexer(
        persist_directory=indexer.persist_directory,
        collection_name=indexer.collection_name,
        enable_persist=True,
    )
    other.reset_collection()

    assert indexer.index_directory(tmp_path, glob_pattern="*.md") == 1
    assert _ids_for(indexer, f)


def test_nonpersistent_replacement_serializes_collection_through_sweep(
    indexer, tmp_path, monkeypatch
):
    # Exercise the no-file-lock branch without changing the real Chroma backend.
    monkeypatch.setattr(indexer, "persist_directory", None)
    _check_serialized_replacement(indexer, tmp_path, monkeypatch, directory=False)


def test_file_replacement_serializes_collection_through_sweep(indexer, tmp_path, monkeypatch):
    _check_serialized_replacement(indexer, tmp_path, monkeypatch, directory=False)


def test_directory_replacement_serializes_collection_through_sweep(indexer, tmp_path, monkeypatch):
    _check_serialized_replacement(indexer, tmp_path, monkeypatch, directory=True)


def _check_serialized_replacement(indexer, tmp_path, monkeypatch, directory):
    """A delayed B replacement must not delete a newer writer's reused A ids."""
    f = tmp_path / "note.md"
    original = _words(200)
    f.write_text(original)

    def reindex():
        if directory:
            return indexer.index_directory(tmp_path, glob_pattern="*.md")
        return indexer.index_file(f)

    reindex()
    original_ids = set(_ids_for(indexer, f))
    snapshotted, release, second_collected = Event(), Event(), Event()
    real_snapshot = indexer._snapshot_source_ids
    real_collect = indexer.collect_documents
    first_snapshot = True

    def paused_snapshot(sources):
        nonlocal first_snapshot
        result = real_snapshot(sources)
        if first_snapshot:
            first_snapshot = False
            snapshotted.set()
            assert release.wait(10), "test never released first writer"
        return result

    def observed_collect(*args, **kwargs):
        result = real_collect(*args, **kwargs)
        if snapshotted.is_set():
            second_collected.set()
        return result

    monkeypatch.setattr(indexer, "_snapshot_source_ids", paused_snapshot)
    monkeypatch.setattr(indexer, "collect_documents", observed_collect)
    f.write_text(original + " generation B")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(reindex)
        try:
            assert snapshotted.wait(10), "first writer never took its snapshot"
            f.write_text(original)
            second = pool.submit(reindex)
            # On the broken path A completes while B is paused. The fixed path
            # must not even collect A until B has finished its replacement.
            interleaved = second_collected.wait(0.5)
            if interleaved:
                second.result(timeout=10)
        finally:
            release.set()
        first.result(timeout=10)
        second.result(timeout=10)

    assert set(_ids_for(indexer, f)) == original_ids, "B deleted the newer writer's A chunks"
    assert not interleaved, "file collection escaped the replacement's writer lock"


def test_sweep_keeps_chunks_added_by_a_concurrent_writer(indexer, tmp_path, monkeypatch):
    """The sweep deletes only ids that existed before this run's add.

    Another writer can add its own chunks for the same source after our
    snapshot. Deleting everything this run did not write would delete that
    writer's chunks; with two writers each doing so the source ends up empty.
    """
    f = tmp_path / "note.md"
    f.write_text(_words(200))
    indexer.index_file(f)
    source = str(f.resolve())

    f.write_text(_words(200) + " changed")
    real_add = indexer.add_documents
    foreign_id = f"{source}@other-writer#chunk0-0"

    def add_then_concurrent_write(docs):
        real_add(docs)
        indexer.collection.add(
            documents=["chunk from another writer"],
            metadatas=[{"source": source}],
            ids=[foreign_id],
        )

    monkeypatch.setattr(indexer, "add_documents", add_then_concurrent_write)
    indexer.index_file(f)

    ids = indexer.collection.get()["ids"]
    assert foreign_id in ids, f"the sweep deleted a concurrent writer's chunk: {ids}"
    assert len([i for i in ids if i.startswith(f"{source}@")]) > 1
