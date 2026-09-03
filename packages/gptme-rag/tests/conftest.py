import pytest

try:
    import chromadb

    _has_chromadb = True
except ImportError:
    _has_chromadb = False


@pytest.fixture(autouse=True)
def isolated_local_embedding_cache(tmp_path, monkeypatch):
    """Keep the local embedding cache out of ~/.cache during tests."""
    monkeypatch.setenv("GPTME_RAG_EMBEDDING_CACHE", str(tmp_path / "local-embeddings.sqlite"))


@pytest.fixture(autouse=True)
def cleanup_chroma():
    """Clean up ChromaDB between tests."""
    yield
    if not _has_chromadb:
        return
    # Reset the ChromaDB client system
    if hasattr(chromadb.api.client.SharedSystemClient, "_identifer_to_system"):
        chromadb.api.client.SharedSystemClient._identifer_to_system = {}


@pytest.fixture
def indexer(request, tmp_path):
    """Create an indexer with a unique collection name based on the test name."""
    if not _has_chromadb:
        pytest.skip("chromadb not installed")
    from gptme_rag.indexing.indexer import Indexer
    import logging

    logger = logging.getLogger(__name__)

    collection_name = request.node.name.replace("[", "_").replace("]", "_")
    idx = Indexer(
        persist_directory=tmp_path / "index",
        chunk_size=50,  # Smaller chunk size to ensure multiple chunks
        chunk_overlap=10,
        enable_persist=True,  # Enable persistent storage
        collection_name=collection_name,  # Unique collection name per test
    )

    # Reset collection before test
    idx.reset_collection()
    logger.debug("Reset collection before test")

    yield idx

    # Cleanup after test
    idx.reset_collection()
    logger.debug("Reset collection after test")
