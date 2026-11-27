"""Chunk lookup and document reconstruction (gptme/gptme-contrib#2562)."""

import pytest

pytest.importorskip("chromadb")

from gptme_rag.indexing import indexer as indexer_module  # noqa: E402
from gptme_rag.indexing.document import Document  # noqa: E402


def _words(n: int) -> str:
    return " ".join(f"w{i}" for i in range(n))


def _base_id(indexer) -> str:
    return indexer.collection.get()["ids"][0].split("#chunk")[0]


def test_reconstruct_document_matches_original(indexer, tmp_path):
    """Reconstruction drops the chunk overlaps instead of repeating them."""
    text = _words(200)
    f = tmp_path / "big.txt"
    f.write_text(text)
    assert indexer.index_file(f) > 1

    doc = indexer.reconstruct_document(_base_id(indexer))

    assert doc.content == text
    assert "chunk_end" not in doc.metadata


def test_reconstruct_document_preserves_newlines(indexer, tmp_path):
    """Chunk text is concatenated, not joined with an invented newline."""
    text = "\n".join(f"line {i} has a few words in it" for i in range(60)) + "\n"
    f = tmp_path / "lines.md"
    f.write_text(text)
    indexer.index_file(f)

    assert indexer.reconstruct_document(_base_id(indexer)).content == text


def test_get_document_chunks_ordered_by_chunk_index(indexer, tmp_path):
    """Chunks come back in chunk_index order whatever the storage order."""
    f = tmp_path / "big.txt"
    text = _words(300)
    f.write_text(text)
    docs = list(Document.from_file(f.resolve(), processor=indexer.processor))
    assert len(docs) > 10
    indexer.add_documents(list(reversed(docs)))
    base_id = docs[0].doc_id.split("#chunk")[0]

    chunks = indexer.get_document_chunks(base_id)

    assert [c.chunk_index for c in chunks] == list(range(len(docs)))
    assert indexer.reconstruct_document(base_id).content == text


def test_get_document_chunks_does_not_match_id_prefix_of_other_doc(indexer, tmp_path):
    """A base id that is a string prefix of another document's id is not merged."""
    a = tmp_path / "a.txt"
    text = _words(120)
    a.write_text(text)
    indexer.index_file(a)
    base_id = _base_id(indexer)
    # Another document whose id extends the first base id without a #chunk suffix.
    indexer.add_documents(
        [Document(content="other", metadata={"source": "x"}, doc_id=base_id + "0")]
    )

    chunks = indexer.get_document_chunks(base_id)

    assert chunks and all(c.doc_id.startswith(base_id + "#chunk") for c in chunks)
    assert indexer.reconstruct_document(base_id).content == text


def test_get_document_chunks_reads_in_pages(indexer, tmp_path, monkeypatch):
    """No unbounded get(), which can exceed SQLite's variable limit."""
    f = tmp_path / "big.txt"
    text = _words(200)
    f.write_text(text)
    n_chunks = indexer.index_file(f)
    base_id = _base_id(indexer)
    monkeypatch.setattr(indexer_module, "_PAGE_SIZE", 3)
    real_get = indexer.collection.get

    def bounded_get(*args, **kwargs):
        ids = kwargs.get("ids")
        size = len(ids) if ids is not None else kwargs.get("limit")
        assert size is not None and size <= 3, f"unbounded get: {kwargs}"
        return real_get(*args, **kwargs)

    monkeypatch.setattr(indexer.collection, "get", bounded_get)

    assert len(indexer.get_document_chunks(base_id)) == n_chunks
    assert indexer.reconstruct_document(base_id).content == text


def test_get_document_chunks_holds_the_writer_lock(indexer, tmp_path):
    """The paged id scan runs under the writer lock, so offsets cannot shift."""
    from gptme_rag.indexing.gc import index_writer_lock

    f = tmp_path / "big.txt"
    f.write_text(_words(200))
    indexer.index_file(f)
    base_id = _base_id(indexer)

    real_get = indexer.collection.get
    seen_locked = []

    def get_while_probing_lock(*args, **kwargs):
        if not seen_locked:
            try:
                with index_writer_lock(indexer.persist_directory, blocking=False):
                    seen_locked.append(False)
            except RuntimeError:
                seen_locked.append(True)
        return real_get(*args, **kwargs)

    indexer.collection.get = get_while_probing_lock  # type: ignore[method-assign]
    try:
        assert indexer.get_document_chunks(base_id)
    finally:
        indexer.collection.get = real_get  # type: ignore[method-assign]

    assert seen_locked == [True], "get_document_chunks read ids without the writer lock"


def test_reconstruct_document_missing_end_metadata_at_seam(indexer, tmp_path):
    """A chunk without chunk_end still has its overlap with the next one removed.

    The end is derived from the start (metadata or id) plus the token count
    (metadata or re-encoding).
    """
    # (Not parametrized: the fixture derives the collection name from the test
    # id, and a trailing "]" is not a valid Chroma collection name.)
    for n, drop in enumerate(
        [
            ("chunk_end",),
            ("chunk_end", "token_count"),
            ("chunk_end", "token_count", "chunk_start"),
        ]
    ):
        f = tmp_path / f"big{n}.txt"
        text = _words(200 + n)
        f.write_text(text)
        docs = list(Document.from_file(f.resolve(), processor=indexer.processor))
        assert len(docs) > 2
        for doc in docs[:-1]:
            for key in drop:
                doc.metadata.pop(key)
        indexer.add_documents(docs)

        got = indexer.reconstruct_document(docs[0].doc_id.split("#chunk")[0]).content
        assert got == text, f"missing {drop}"


def test_reconstruct_document_dedups_overlap_on_repetitive_content(indexer, tmp_path):
    """The overlap is removed without swallowing a whole repetitive chunk."""
    text = "----\n" * 400
    f = tmp_path / "repetitive.txt"
    f.write_text(text, encoding="utf-8")
    assert indexer.index_file(f) > 1

    assert indexer.reconstruct_document(_base_id(indexer)).content == text


def test_reconstruct_document_keeps_content_when_overlap_check_fails(indexer, tmp_path):
    """The fallback must not match a whole repetitive chunk and drop it.

    Tiny chunks split the emoji across token boundaries, so re-encoding cannot
    locate the overlap exactly and the bounded fallback runs. Content must
    never be dropped, and duplication stays bounded.
    """
    indexer.processor.chunk_size = 7
    indexer.processor.chunk_overlap = 3
    f = tmp_path / "emoji.txt"
    f.write_text("\U0001f600" * 100, encoding="utf-8")
    n_chunks = indexer.index_file(f)

    doc = indexer.reconstruct_document(_base_id(indexer))

    kept = doc.content.count("\U0001f600")
    assert kept >= 100, "content was dropped"
    assert kept <= 100 + n_chunks, f"overlap duplicated per chunk: {kept} for {n_chunks} chunks"


@pytest.mark.parametrize(
    ("left", "right", "expected", "overlap"),
    [
        # Identical lines match at every period: take the one near the expected size.
        ("----\n" * 10, "----\n" * 10, 9, "----\n" * 2),
        # Never the whole next chunk, even when that is the only length in range.
        ("ab" * 4, "ab" * 4, 8, "ab" * 3),
        # Nothing within twice the expected overlap: drop nothing.
        ("x" + "abcdefgh", "abcdefgh" + "y", 3, ""),
    ],
)
def test_overlap_near_is_bounded_by_expected_overlap(left, right, expected, overlap):
    assert indexer_module._overlap_near(left, right, expected) == overlap
