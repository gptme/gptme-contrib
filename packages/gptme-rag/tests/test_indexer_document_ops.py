"""Indexer document operations: emptied files, delete_document, chunk reconstruction.

Regression tests for gptme/gptme-contrib#2356 R1-R3.
"""

from gptme_rag.indexing.document import Document


def _words(n: int) -> str:
    return " ".join(f"w{i}" for i in range(n))


def _base_id(indexer) -> str:
    return indexer.collection.get()["ids"][0].split("#chunk")[0]


def test_index_directory_drops_emptied_file(indexer, tmp_path):
    """R1: an emptied file disappears from the index instead of becoming an empty doc."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    f = corpus / "note.md"
    f.write_text("hello world, some real content")
    indexer.index_directory(corpus, glob_pattern="**/*.md")
    f.write_text("")

    assert indexer.index_directory(corpus, glob_pattern="**/*.md") == 1

    got = indexer.collection.get(where={"source": str(f.resolve())})
    assert got["ids"] == [], f"tombstone stored as a document: {got['documents']!r}"


def test_index_file_drops_emptied_file(indexer, tmp_path):
    """R1: index_file on an emptied file removes its chunks and stores nothing."""
    f = tmp_path / "note.md"
    f.write_text(_words(200))
    assert indexer.index_file(f) > 1
    f.write_text("   \n")

    assert indexer.index_file(f) == 0

    got = indexer.collection.get(where={"source": str(f.resolve())})
    assert got["ids"] == [], f"left behind: {got['documents']!r}"


def test_delete_document_by_base_id_deletes_chunks(indexer, tmp_path):
    """R2: delete_document(base_id) deletes the document's chunks."""
    keep = tmp_path / "keep.txt"
    keep.write_text(_words(120))
    indexer.index_file(keep)
    keep_ids = set(indexer.collection.get()["ids"])

    f = tmp_path / "big.txt"
    f.write_text(_words(200))
    indexer.index_file(f)
    base_id = next(i for i in indexer.collection.get()["ids"] if i not in keep_ids).split("#chunk")[
        0
    ]

    assert indexer.delete_document(base_id) is True
    assert set(indexer.collection.get()["ids"]) == keep_ids


def test_delete_document_unknown_id_returns_false(indexer, tmp_path):
    """R2: deleting an id that matches nothing reports False."""
    f = tmp_path / "big.txt"
    f.write_text(_words(200))
    indexer.index_file(f)
    before = indexer.collection.get()["ids"]

    assert indexer.delete_document("/no/such/file@deadbeef") is False
    assert indexer.collection.get()["ids"] == before


def test_reconstruct_document_matches_original(indexer, tmp_path):
    """R3: reconstruction drops the chunk overlaps instead of repeating them."""
    text = _words(200)
    f = tmp_path / "big.txt"
    f.write_text(text)
    indexer.index_file(f)

    doc = indexer.reconstruct_document(_base_id(indexer))

    assert doc.content == text


def test_reconstruct_document_preserves_newlines(indexer, tmp_path):
    """R3: chunk text is concatenated, not joined with an invented newline."""
    text = "\n".join(f"line {i} has a few words in it" for i in range(60)) + "\n"
    f = tmp_path / "lines.md"
    f.write_text(text)
    indexer.index_file(f)

    assert indexer.reconstruct_document(_base_id(indexer)).content == text


def test_get_document_chunks_ordered_by_chunk_index(indexer, tmp_path):
    """R3: chunks come back in chunk_index order whatever the storage order."""
    f = tmp_path / "big.txt"
    f.write_text(_words(300))
    docs = list(Document.from_file(f.resolve(), processor=indexer.processor))
    assert len(docs) > 10
    indexer.add_documents(list(reversed(docs)))
    base_id = docs[0].doc_id.split("#chunk")[0]

    chunks = indexer.get_document_chunks(base_id)

    order = [c.chunk_index for c in chunks]
    assert order == list(range(len(docs)))


def test_get_document_chunks_does_not_match_id_prefix_of_other_doc(indexer, tmp_path):
    """R3: a base id that is a string prefix of another document's id is not merged."""
    a = tmp_path / "a.txt"
    a.write_text(_words(120))
    indexer.index_file(a)
    base_id = _base_id(indexer)
    # A second document whose id extends the first base id without a #chunk suffix.
    indexer.add_documents(
        [Document(content="other", metadata={"source": "x"}, doc_id=base_id + "0")]
    )

    chunks = indexer.get_document_chunks(base_id)

    assert chunks and all(c.doc_id.startswith(base_id + "#chunk") for c in chunks)
