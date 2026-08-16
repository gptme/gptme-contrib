from pathlib import Path

import pytest

from gptme_rag.indexing.indexer import Indexer


def test_force_recreate_clears_unavailable_stored_model_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Force-recreating an unavailable auto-detected model permits indexing again."""
    import chromadb
    from chromadb.config import Settings

    persist_dir = tmp_path / "index"
    persist_dir.mkdir()
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    (doc_dir / "test.txt").write_text("hello world")

    settings = Settings(allow_reset=True, is_persistent=True, anonymized_telemetry=False)
    client = chromadb.PersistentClient(path=str(persist_dir), settings=settings)
    client.create_collection(
        name="default",
        metadata={
            "hnsw:space": "cosine",
            "embedding_model": "openai/text-embedding-3-large",
            "embedding_backend": "openrouter",
        },
    )
    del client

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY_EVAL", raising=False)
    indexer = Indexer(
        persist_directory=persist_dir,
        enable_persist=True,
        embedding_function="auto",
        collection_name="default",
        force_recreate=True,
    )

    assert indexer._stored_model_name is None
    assert indexer.index_directory(doc_dir) == 1
