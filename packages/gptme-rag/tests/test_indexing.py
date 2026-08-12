from datetime import datetime
from pathlib import Path

import pytest
import gptme_rag.indexing.indexer as indexer_module
from gptme_rag.indexing.document import Document
from gptme_rag.indexing.indexer import Indexer


@pytest.fixture
def test_docs():
    return [
        Document(
            content="This is a test document about Python programming.",
            metadata={"source": "test1.txt", "category": "programming"},
            doc_id="1",
        ),
        Document(
            content="Another document discussing machine learning.",
            metadata={"source": "test2.txt", "category": "ml"},
            doc_id="2",
        ),
    ]


def test_document_from_file(tmp_path):
    # Create a test file
    test_file = tmp_path / "test.txt"
    test_content = "Test content"
    test_file.write_text(test_content)

    # Create document from file
    docs = list(Document.from_file(test_file))
    assert len(docs) > 0
    doc = docs[0]  # Get the first document

    assert doc.content == test_content
    assert doc.source_path == test_file
    assert doc.metadata["filename"] == "test.txt"
    assert doc.metadata["extension"] == ".txt"


def test_indexer_add_document(indexer, test_docs):
    # Add single document
    indexer.add_document(test_docs[0])
    results, distances, _ = indexer.search("Python programming")

    assert len(results) > 0
    assert "Python programming" in results[0].content
    assert len(distances) > 0


def test_indexer_add_documents(indexer, test_docs):
    # Reset collection to ensure clean state
    indexer.reset_collection()

    # Add multiple documents
    indexer.add_documents(test_docs)

    # Verify documents were added
    results = indexer.collection.get()
    assert len(results["documents"]) == len(test_docs), "Not all documents were added"

    # Search for programming-related content
    prog_results, prog_distances, _ = indexer.search("programming")
    assert len(prog_results) > 0
    assert any("Python" in doc.content for doc in prog_results)
    assert len(prog_distances) > 0

    # Search for ML-related content
    ml_results, ml_distances, _ = indexer.search("machine learning")
    assert len(ml_results) > 0, "No results found for 'machine learning'"
    assert any("machine learning" in doc.content.lower() for doc in ml_results), (
        f"Expected 'machine learning' in results: {[doc.content for doc in ml_results]}"
    )
    assert len(ml_distances) > 0, "No distances returned"


def test_indexer_directory(indexer, tmp_path):
    # Create test files in different directories with different extensions
    docs_dir = tmp_path / "docs"
    src_dir = tmp_path / "src"
    docs_dir.mkdir()
    src_dir.mkdir()

    # Create markdown files in docs
    (docs_dir / "guide.md").write_text("Python programming guide")
    (docs_dir / "tutorial.md").write_text("JavaScript tutorial")

    # Create Python files in src
    (src_dir / "main.py").write_text("def main(): print('Hello')")
    (src_dir / "utils.py").write_text("def util(): return True")

    # Create a text file in root
    (tmp_path / "notes.txt").write_text("Random notes")

    # Index everything
    indexer.index_directory(tmp_path)

    # Test extension filter (*.md)
    md_results, _, _ = indexer.search(
        "programming",
        path_filters=("*.md",),
    )
    assert len(md_results) > 0
    assert all(doc.metadata["source"].endswith(".md") for doc in md_results)

    # Test directory pattern (src/*.py)
    py_results, _, _ = indexer.search(
        "def",
        path_filters=(str(src_dir / "*.py"),),
    )
    assert len(py_results) > 0
    assert all(
        Path(doc.metadata["source"]).parent.name == "src" and doc.metadata["source"].endswith(".py")
        for doc in py_results
    )

    # Test multiple patterns
    multi_results, _, _ = indexer.search(
        "programming",
        path_filters=("*.md", "*.py"),
    )
    assert len(multi_results) > 0
    assert all(doc.metadata["source"].endswith((".md", ".py")) for doc in multi_results)

    # Test with path and filter combined
    docs_md_results, _, _ = indexer.search(
        "tutorial",
        paths=[docs_dir],
        path_filters=("*.md",),
    )
    assert len(docs_md_results) > 0
    assert all(
        Path(doc.metadata["source"]).parent.name == "docs"
        and doc.metadata["source"].endswith(".md")
        for doc in docs_md_results
    )


def test_indexer_directory_is_idempotent(indexer, tmp_path):
    """Repeated directory indexing should replace existing chunks, not duplicate them."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "guide.md").write_text("Python programming guide")
    (docs_dir / "tutorial.md").write_text("JavaScript tutorial")

    assert indexer.index_directory(docs_dir, glob_pattern="**/*.md") == 2
    first = indexer.collection.get()
    assert len(first["ids"]) > 0

    assert indexer.index_directory(docs_dir, glob_pattern="**/*.md") == 2
    second = indexer.collection.get()

    assert len(second["ids"]) == len(first["ids"])
    assert len(set(second["ids"])) == len(second["ids"])


def test_path_matching(indexer):
    # Test the _matches_paths method directly
    doc = Document(
        content="test",
        metadata={"source": "/home/user/project/docs/guide.md"},
        doc_id="test",
    )

    # Test simple extension filter
    assert indexer._matches_paths(doc, path_filters=("*.md",))
    assert not indexer._matches_paths(doc, path_filters=("*.py",))

    # Test directory pattern
    assert indexer._matches_paths(doc, path_filters=("docs/*.md",))
    assert not indexer._matches_paths(doc, path_filters=("src/*.md",))

    # Test multiple patterns
    assert indexer._matches_paths(doc, path_filters=("*.py", "*.md"))
    assert indexer._matches_paths(doc, path_filters=("src/*.py", "docs/*.md"))

    # Test with exact paths
    assert indexer._matches_paths(doc, paths=[Path("/home/user/project/docs")])
    assert not indexer._matches_paths(doc, paths=[Path("/home/user/project/src")])

    # Test combining paths and filters
    assert indexer._matches_paths(
        doc,
        paths=[Path("/home/user/project/docs")],
        path_filters=("*.md",),
    )
    assert not indexer._matches_paths(
        doc,
        paths=[Path("/home/user/project/docs")],
        path_filters=("*.py",),
    )


def test_add_document_failure_does_not_wipe_collection(indexer, test_docs, monkeypatch):
    """A failed add must raise, not destroy the existing index (data-loss regression)."""
    indexer.add_document(test_docs[0])

    def boom(*args, **kwargs):
        raise RuntimeError("simulated add failure")

    monkeypatch.setattr(indexer.collection, "add", boom)
    with pytest.raises(RuntimeError, match="simulated add failure"):
        indexer.add_document(test_docs[1])

    got = indexer.collection.get(ids=[test_docs[0].doc_id])
    assert len(got["ids"]) == 1, "existing documents must survive a failed add"


def test_delete_documents_failure_does_not_wipe_collection(indexer, test_docs, monkeypatch):
    """A failed delete must raise, not escalate into deleting the whole collection."""
    indexer.add_documents(test_docs)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated delete failure")

    monkeypatch.setattr(indexer.collection, "delete", boom)
    with pytest.raises(RuntimeError, match="simulated delete failure"):
        indexer.delete_documents({"category": "ml"})

    got = indexer.collection.get()
    assert len(got["ids"]) == len(test_docs), "documents must survive a failed delete"


def test_generated_doc_id_is_stable():
    """Documents without explicit IDs should not duplicate across processes."""
    indexer = Indexer(embedding_function="default")
    doc_a = Document(
        content="same content",
        metadata={"source": "/tmp/a.md", "chunk_index": 0},
    )
    doc_b = Document(
        content="same content",
        metadata={"source": "/tmp/a.md", "chunk_index": 0},
    )
    doc_c = Document(
        content="same content",
        metadata={"source": "/tmp/b.md", "chunk_index": 0},
    )

    assert indexer._generate_doc_id(doc_a).doc_id == indexer._generate_doc_id(doc_b).doc_id
    assert indexer._generate_doc_id(doc_a).doc_id != indexer._generate_doc_id(doc_c).doc_id


def test_compute_relevance_score_handles_empty_query():
    """Explain-mode scoring should not divide by zero for an empty query."""
    indexer = Indexer(embedding_function="default")
    doc = Document(content="content", metadata={"source": "test.txt"}, doc_id="doc")

    score, scores = indexer.compute_relevance_score(doc, distance=0.2, query="")
    explanation = indexer.explain_scoring("", doc, 0.2, scores)

    assert score == sum(scores.values())
    assert scores["term_overlap"] == 0.0
    assert explanation["explanations"]["term_overlap"].startswith("Term overlap 0.0%")


def test_compute_relevance_score_accepts_iso_last_modified():
    """Document.from_file stores ISO metadata; recency scoring must parse it."""
    indexer = Indexer(embedding_function="default")
    doc = Document(
        content="fresh content",
        metadata={
            "source": "fresh.txt",
            "last_modified": datetime.now().isoformat(),
        },
        doc_id="fresh",
    )

    score, scores = indexer.compute_relevance_score(doc, distance=0.2, query="fresh")
    explanation = indexer.explain_scoring("fresh", doc, 0.2, scores)

    assert score > scores["base"]
    assert scores["recency_boost"] > 0.0
    assert explanation["explanations"]["recency_boost"].startswith("Modified ")


def test_auto_embedding_mode_does_not_destroy_foreign_model_collection(tmp_path):
    """Indexer with embedding_function='auto' must not silently recreate a collection
    that was indexed with a non-default model (e.g. OpenRouter).

    Regression test for P0 data-loss bug: status/search commands default to
    'auto' but previously passed 'modernbert', which triggered the mismatch
    guard and deleted all indexed content.
    """
    import chromadb
    from chromadb.config import Settings

    persist_dir = tmp_path / "index"
    persist_dir.mkdir()

    # Simulate a collection created by the OpenRouter embedding backend
    settings = Settings(allow_reset=True, is_persistent=True, anonymized_telemetry=False)
    client = chromadb.PersistentClient(path=str(persist_dir), settings=settings)
    fake_model = "openai/text-embedding-3-large"
    col = client.create_collection(
        name="default",
        metadata={"hnsw:space": "cosine", "embedding_model": fake_model},
    )
    # Add a sentinel document with a raw embedding (dim=384 matches MiniLM)
    col.add(ids=["sentinel"], embeddings=[[0.0] * 384], documents=["sentinel doc"])
    assert col.count() == 1

    # Open via auto mode (simulates what `gptme-rag status` / `search` does)
    indexer = Indexer(
        persist_directory=persist_dir,
        enable_persist=True,
        embedding_function="auto",
        collection_name="default",
    )

    # Collection must still have the sentinel document — not been recreated
    assert indexer.collection.count() == 1, (
        "auto mode destroyed the collection indexed with a non-default embedding model"
    )


def test_auto_embedding_mode_rebinds_sentence_transformer_collection(tmp_path, monkeypatch):
    """Auto mode must reopen sentence-transformer collections with the matching embedder."""

    class FakeModernBERTEmbedding:
        is_msmarco = False

        def __init__(self, model_name="modernbert", device="cpu"):
            self.model_name = model_name

        @staticmethod
        def name() -> str:
            return "fake-modernbert"

        @staticmethod
        def is_legacy() -> bool:
            return False

        def __call__(self, input):
            return [[1.0] * 768 for _ in input]

        def embed_query(self, input):
            return self(input)

        def embed_documents(self, input):
            return self(input)

    class FakeSentenceTransformerEmbedding:
        is_msmarco = False

        def __init__(self, model_name, device="cpu"):
            self.model_name = model_name

        @staticmethod
        def name() -> str:
            return "fake-sentence-transformer"

        @staticmethod
        def is_legacy() -> bool:
            return False

        def __call__(self, input):
            return [[0.5] * 384 for _ in input]

        def embed_query(self, input):
            return self(input)

        def embed_documents(self, input):
            return self(input)

    monkeypatch.setattr(indexer_module, "ModernBERTEmbedding", FakeModernBERTEmbedding)
    monkeypatch.setattr(
        indexer_module,
        "GenericSentenceTransformerEmbedding",
        FakeSentenceTransformerEmbedding,
    )

    persist_dir = tmp_path / "index"
    source_indexer = Indexer(
        persist_directory=persist_dir,
        enable_persist=True,
        embedding_function="minilm",
        collection_name="default",
    )
    source_indexer.add_document(
        Document(
            content="Sentence-transformer backed document",
            metadata={"source": str(tmp_path / "minilm.txt")},
            doc_id="minilm-doc",
        )
    )

    reopened = Indexer(
        persist_directory=persist_dir,
        enable_persist=True,
        embedding_function="auto",
        collection_name="default",
    )

    results, _, _ = reopened.search("sentence-transformer")
    assert reopened.collection.count() == 1
    assert reopened.embedding_model_name == "all-MiniLM-L6-v2"
    assert len(results) == 1
    assert results[0].doc_id == "minilm-doc"


def test_auto_embedding_mode_preserves_legacy_chromadb_default_collection(tmp_path):
    """Auto mode must not corrupt a legacy collection that has no embedding_model metadata.

    Regression test for P1 bug: metadata.get("embedding_model", "modernbert") defaulted
    to 'modernbert' for metadata-free collections, so auto mode rebound to ModernBERT
    (768-dim) and ChromaDB raised a dimension mismatch on any subsequent search.
    Fix: default to 'default' so no-metadata collections fall through to the
    reuse_auto_peeked_collection path (embedding_function=None).
    """
    import chromadb
    from chromadb.config import Settings

    persist_dir = tmp_path / "index"
    persist_dir.mkdir()

    # Simulate a legacy collection created before embedding_model metadata was introduced.
    settings = Settings(allow_reset=True, is_persistent=True, anonymized_telemetry=False)
    client = chromadb.PersistentClient(path=str(persist_dir), settings=settings)
    col = client.create_collection(
        name="default",
        metadata={"hnsw:space": "cosine"},  # no embedding_model or embedding_backend
    )
    # ChromaDB default embeddings are 384-dim (all-MiniLM-L6-v2).
    col.add(ids=["legacy-doc"], embeddings=[[0.1] * 384], documents=["legacy content"])
    assert col.count() == 1

    # Open with auto mode — must not remap to ModernBERT (768-dim)
    indexer = Indexer(
        persist_directory=persist_dir,
        enable_persist=True,
        embedding_function="auto",
        collection_name="default",
    )

    # Collection must be intact and reachable
    assert indexer.collection.count() == 1, (
        "auto mode destroyed legacy no-metadata collection (dimension mismatch bug)"
    )


def test_auto_embedding_mode_preserves_collection_when_sentence_transformer_fails_to_load(
    tmp_path, monkeypatch
):
    """Auto mode must not delete a sentence-transformer collection when the model can't load.

    Regression test for P0 bug: if GenericSentenceTransformerEmbedding raised (e.g., model
    download failure, missing weights), the exception propagated out of the Indexer constructor,
    crashing the command. Fix: wrap in try/except and fall back to reuse_auto_peeked_collection,
    preserving the collection without rebinding (same pattern as the OpenRouter API-key fallback).
    """
    import chromadb as _chromadb
    from chromadb.config import Settings

    persist_dir = tmp_path / "index"
    persist_dir.mkdir()

    # Create a persisted collection tagged as sentence-transformers backend.
    settings = Settings(allow_reset=True, is_persistent=True, anonymized_telemetry=False)
    client = _chromadb.PersistentClient(path=str(persist_dir), settings=settings)
    col = client.create_collection(
        name="default",
        metadata={
            "hnsw:space": "cosine",
            "embedding_model": "all-MiniLM-L6-v2",
            "embedding_backend": "sentence-transformers",
        },
    )
    col.add(ids=["st-doc"], embeddings=[[0.5] * 384], documents=["sentence-transformer content"])
    assert col.count() == 1
    del client

    # Simulate a model-load failure (e.g., missing download or corrupt weights).
    # Must be a class (not a plain function) so isinstance() checks in the indexer
    # receive a valid type and return False rather than raising TypeError.
    class _Raise:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("Model weights not found")

    monkeypatch.setattr(indexer_module, "GenericSentenceTransformerEmbedding", _Raise)

    # Opening with auto mode must NOT crash or delete the collection.
    indexer = Indexer(
        persist_directory=persist_dir,
        enable_persist=True,
        embedding_function="auto",
        collection_name="default",
    )
    assert indexer.collection.count() == 1, (
        "auto mode deleted sentence-transformer collection when the model failed to load"
    )


def test_auto_embedding_mode_sentence_transformer_namespace_not_misrouted_to_openrouter(tmp_path):
    """Auto mode must not route 'sentence-transformers/<model>' to the OpenRouter backend.

    Regression test for P1 bug: the slash heuristic used 'org/model' as a signal
    for OpenRouter model names.  Legacy collections indexed with a model from the
    'sentence-transformers/' HuggingFace namespace (e.g. paraphrase-MiniLM-L6-v2)
    have a '/' in their stored embedding_model but no embedding_backend metadata.
    The old heuristic treated any '/'-containing name as OpenRouter, which (without
    an API key) fell back to ModernBERT (768-dim) and caused dimension mismatches.
    Fix: _looks_like_openrouter_model() now excludes known sentence-transformer
    namespaces from the OpenRouter routing path.
    """
    import chromadb
    from chromadb.config import Settings

    persist_dir = tmp_path / "index"
    persist_dir.mkdir()

    settings = Settings(allow_reset=True, is_persistent=True, anonymized_telemetry=False)
    client = chromadb.PersistentClient(path=str(persist_dir), settings=settings)
    # Legacy collection: has a namespaced ST model name but NO embedding_backend.
    col = client.create_collection(
        name="default",
        metadata={
            "hnsw:space": "cosine",
            "embedding_model": "sentence-transformers/paraphrase-MiniLM-L6-v2",
            # intentionally no "embedding_backend" key — this is the legacy format
        },
    )
    col.add(ids=["para-doc"], embeddings=[[0.3] * 384], documents=["paraphrase doc"])
    assert col.count() == 1
    del client

    # Without OPENROUTER_API_KEY, misrouting to OpenRouter would cause a ValueError
    # in OpenRouterEmbedding.__init__ and then fall through to ModernBERT (768-dim),
    # causing a dimension mismatch on any later query against the 384-dim collection.
    # The correct path: treat 'sentence-transformers/*' as sentence-transformers,
    # not OpenRouter, so the collection is preserved (even if the model can't load).
    import os

    env_backup = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        indexer = Indexer(
            persist_directory=persist_dir,
            enable_persist=True,
            embedding_function="auto",
            collection_name="default",
        )
        assert indexer.collection.count() == 1, (
            "auto mode destroyed legacy sentence-transformers/ namespace collection (slash misrouting bug)"
        )
    finally:
        if env_backup is not None:
            os.environ["OPENROUTER_API_KEY"] = env_backup


def test_auto_embedding_mode_baai_namespace_not_misrouted_to_openrouter(tmp_path):
    """Auto mode must not route 'BAAI/<model>' to the OpenRouter backend.

    Regression test for the incomplete namespace list: the initial fix only excluded
    'sentence-transformers/' but left BAAI/, intfloat/, thenlper/ etc. exposed.
    Legacy collections indexed with e.g. 'BAAI/bge-large-en-v1.5' would be misrouted
    to OpenRouter (slash heuristic), causing cryptic errors when the API key is absent.
    Fix: _SENTENCE_TRANSFORMER_NAMESPACES now includes BAAI/ and other common HF
    embedding namespaces.
    """
    import os

    import chromadb
    from chromadb.config import Settings

    persist_dir = tmp_path / "index"
    persist_dir.mkdir()

    settings = Settings(allow_reset=True, is_persistent=True, anonymized_telemetry=False)
    client = chromadb.PersistentClient(path=str(persist_dir), settings=settings)
    col = client.create_collection(
        name="default",
        metadata={
            "hnsw:space": "cosine",
            "embedding_model": "BAAI/bge-large-en-v1.5",
            # intentionally no "embedding_backend" key — legacy format before this PR
        },
    )
    col.add(ids=["bge-doc"], embeddings=[[0.1] * 1024], documents=["bge document"])
    assert col.count() == 1
    del client

    env_backup = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        indexer = Indexer(
            persist_directory=persist_dir,
            enable_persist=True,
            embedding_function="auto",
            collection_name="default",
        )
        assert indexer.collection.count() == 1, (
            "auto mode destroyed legacy BAAI/ namespace collection (incomplete namespace list)"
        )
    finally:
        if env_backup is not None:
            os.environ["OPENROUTER_API_KEY"] = env_backup


def test_auto_mode_unknown_namespace_hf_model_tried_as_st_first(tmp_path, monkeypatch):
    """Auto mode must try to load unknown-namespace models as ST before routing to OR.

    Regression test for P1 heuristic incompleteness: _looks_like_openrouter_model()
    returns True for any 'org/model' name not in _SENTENCE_TRANSFORMER_NAMESPACES.
    A legacy collection indexed with e.g. 'joe32140/ModernBERT-base-msmarco' (a real
    HF model whose namespace is not in the allow-list) would previously be misrouted
    to the OpenRouter backend — causing a cryptic dimension-mismatch when an API key
    is set but the model doesn't exist on OpenRouter.

    Fix: for legacy collections (no embedding_backend metadata), the auto-detect path
    now tries GenericSentenceTransformerEmbedding first; OpenRouter is only attempted
    when ST raises.
    """
    import os

    import chromadb
    from chromadb.config import Settings

    import gptme_rag.indexing.indexer as indexer_module

    # Unknown namespace — not in _SENTENCE_TRANSFORMER_NAMESPACES but IS a real HF model.
    unknown_ns_model = "joe32140/ModernBERT-base-msmarco"

    # Track which backends were attempted.
    st_attempts: list[str] = []
    or_attempts: list[str] = []
    _RealST = indexer_module.GenericSentenceTransformerEmbedding
    _RealOR = indexer_module.OpenRouterEmbedding

    class TrackingST:
        model_name = unknown_ns_model
        is_msmarco = False

        def __init__(self, model_name, device="cpu"):
            st_attempts.append(model_name)
            self.model_name = model_name

        def __call__(self, input):
            return [[0.25] * 384 for _ in input]

        def embed_query(self, input):
            return self(input)

        def embed_documents(self, input):
            return self(input)

        @staticmethod
        def name():
            return "tracking-st"

        @staticmethod
        def is_legacy():
            return False

    class TrackingOR:
        def __init__(self, model_name=None, **kwargs):
            or_attempts.append(model_name)
            raise ValueError("Tracking OR: intentionally unavailable")

    monkeypatch.setattr(indexer_module, "GenericSentenceTransformerEmbedding", TrackingST)
    monkeypatch.setattr(indexer_module, "OpenRouterEmbedding", TrackingOR)

    persist_dir = tmp_path / "index"
    persist_dir.mkdir()

    settings = Settings(allow_reset=True, is_persistent=True, anonymized_telemetry=False)
    client = chromadb.PersistentClient(path=str(persist_dir), settings=settings)
    col = client.create_collection(
        name="default",
        metadata={
            "hnsw:space": "cosine",
            "embedding_model": unknown_ns_model,
            # intentionally no "embedding_backend" — legacy format
        },
    )
    col.add(ids=["doc"], embeddings=[[0.25] * 384], documents=["some doc"])
    assert col.count() == 1
    del client

    env_backup = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        # Open with auto mode — with an API key set to confirm the OR path is NOT taken first.
        os.environ["OPENROUTER_API_KEY"] = "test-key"
        indexer = Indexer(
            persist_directory=persist_dir,
            enable_persist=True,
            embedding_function="auto",
            collection_name="default",
        )
        # The collection must still be intact — not destroyed.
        assert indexer.collection.count() == 1, (
            "auto mode destroyed unknown-namespace HF model collection"
        )
        # ST must have been tried first.
        assert st_attempts == [unknown_ns_model], (
            f"Expected ST to be tried first for unknown namespace; st_attempts={st_attempts}"
        )
        # OR must NOT have been attempted (ST succeeded).
        assert or_attempts == [], (
            f"OR should not be attempted when ST succeeds; or_attempts={or_attempts}"
        )
    finally:
        os.environ.pop("OPENROUTER_API_KEY", None)
        if env_backup is not None:
            os.environ["OPENROUTER_API_KEY"] = env_backup


def test_reset_collection_preserves_embedding_metadata(indexer):
    """reset_collection must recreate with the same embedding model metadata."""
    indexer.reset_collection()
    metadata = indexer.collection.metadata or {}
    assert metadata.get("embedding_model") == indexer.embedding_model_name
    assert metadata.get("embedding_backend") == indexer.embedding_backend_name


def test_auto_mode_search_raises_clear_error_when_openrouter_key_absent(tmp_path):
    """search() must raise RuntimeError with a clear message, not a cryptic ChromaDB
    dimension-mismatch, when the stored OpenRouter model could not be re-loaded.

    Regression test for P1: embedding_function=None with a non-default stored model
    caused search() to fall through to query_texts (ChromaDB default 384-dim MiniLM),
    which mismatched the stored OpenRouter 3072-dim vectors.
    """
    import os

    import chromadb
    from chromadb.config import Settings

    persist_dir = tmp_path / "index"
    persist_dir.mkdir()

    settings = Settings(allow_reset=True, is_persistent=True, anonymized_telemetry=False)
    client = chromadb.PersistentClient(path=str(persist_dir), settings=settings)
    col = client.create_collection(
        name="default",
        metadata={
            "hnsw:space": "cosine",
            "embedding_model": "openai/text-embedding-3-large",
            "embedding_backend": "openrouter",
        },
    )
    col.add(
        ids=["or-doc"],
        embeddings=[[0.1] * 3072],
        documents=["an openrouter-indexed document"],
    )
    assert col.count() == 1
    del client

    env_backup = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        indexer = Indexer(
            persist_directory=persist_dir,
            enable_persist=True,
            embedding_function="auto",
            collection_name="default",
        )
        # Collection must be preserved (P0 regression check).
        assert indexer.collection.count() == 1
        # The embedding_model_name should reflect the actual stored model, not "default" (P2).
        assert indexer.embedding_model_name == "openai/text-embedding-3-large"
        # search() must raise a clear RuntimeError, not a cryptic ChromaDB error (P1).
        with pytest.raises(RuntimeError, match="openai/text-embedding-3-large"):
            indexer.search("test query")
    finally:
        if env_backup is not None:
            os.environ["OPENROUTER_API_KEY"] = env_backup


def test_auto_mode_search_raises_clear_error_when_sentence_transformer_fails(tmp_path, monkeypatch):
    """search() must raise RuntimeError with a clear message when the stored
    sentence-transformer model could not be re-loaded.

    Regression test for P1: same as OpenRouter case but for sentence-transformer backend.
    """
    import chromadb
    from chromadb.config import Settings

    persist_dir = tmp_path / "index"
    persist_dir.mkdir()

    settings = Settings(allow_reset=True, is_persistent=True, anonymized_telemetry=False)
    client = chromadb.PersistentClient(path=str(persist_dir), settings=settings)
    col = client.create_collection(
        name="default",
        metadata={
            "hnsw:space": "cosine",
            "embedding_model": "all-mpnet-base-v2",
            "embedding_backend": "sentence-transformers",
        },
    )
    col.add(
        ids=["st-doc"],
        embeddings=[[0.2] * 768],
        documents=["an mpnet-indexed document"],
    )
    assert col.count() == 1
    del client

    class _Raise:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("Model weights not found")

    monkeypatch.setattr(indexer_module, "GenericSentenceTransformerEmbedding", _Raise)

    indexer = Indexer(
        persist_directory=persist_dir,
        enable_persist=True,
        embedding_function="auto",
        collection_name="default",
    )
    # Collection must be preserved.
    assert indexer.collection.count() == 1
    # embedding_model_name should reflect the stored model name, not "default" (P2).
    assert indexer.embedding_model_name == "all-mpnet-base-v2"
    # search() must raise a clear RuntimeError, not a ChromaDB dimension-mismatch (P1).
    with pytest.raises(RuntimeError, match="all-mpnet-base-v2"):
        indexer.search("test query")


def test_auto_mode_add_document_raises_clear_error_when_stored_model_failed(tmp_path):
    """add_document() must raise RuntimeError when the stored model could not be loaded.

    Regression test for P1: when auto-mode sets embedding_function=None because
    the stored model (e.g. OpenRouter without API key) could not be re-loaded,
    calling add_document would pass embedding_function=None to ChromaDB, causing
    it to use default 384-dim MiniLM — resulting in a cryptic dimension-mismatch
    error against 3072-dim stored vectors instead of a clear user-facing message.
    """
    import os

    import chromadb
    from chromadb.config import Settings

    persist_dir = tmp_path / "index"
    persist_dir.mkdir()

    settings = Settings(allow_reset=True, is_persistent=True, anonymized_telemetry=False)
    client = chromadb.PersistentClient(path=str(persist_dir), settings=settings)
    col = client.create_collection(
        name="default",
        metadata={
            "hnsw:space": "cosine",
            "embedding_model": "openai/text-embedding-3-large",
            "embedding_backend": "openrouter",
        },
    )
    col.add(
        ids=["or-doc"],
        embeddings=[[0.1] * 3072],
        documents=["an openrouter-indexed document"],
    )
    assert col.count() == 1
    del client

    env_backup = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        indexer = Indexer(
            persist_directory=persist_dir,
            enable_persist=True,
            embedding_function="auto",
            collection_name="default",
        )
        # Collection must be preserved (P0 regression check).
        assert indexer.collection.count() == 1
        # add_document must raise a clear RuntimeError, not a cryptic ChromaDB
        # dimension-mismatch (P1).
        from gptme_rag.indexing.document import Document

        with pytest.raises(RuntimeError, match="openai/text-embedding-3-large"):
            indexer.add_document(Document(content="new doc", metadata={"source": "test.txt"}))
        # Collection must still be intact after the failed add attempt.
        assert indexer.collection.count() == 1

        # add_documents (batch path via _add_documents) must also raise a clear
        # RuntimeError, not a cryptic ChromaDB dimension-mismatch (P1 extension).
        with pytest.raises(RuntimeError, match="openai/text-embedding-3-large"):
            indexer.add_documents([Document(content="new doc", metadata={"source": "test.txt"})])
        assert indexer.collection.count() == 1
    finally:
        if env_backup is not None:
            os.environ["OPENROUTER_API_KEY"] = env_backup


def test_auto_mode_rebinds_openrouter_collection_when_api_key_present(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """auto mode must construct OpenRouterEmbedding and successfully search when
    an API key is available and the stored collection uses the openrouter backend.

    This is the key-present path — the counterpart to
    test_auto_mode_search_raises_clear_error_when_openrouter_key_absent.
    A regression in model-name threading or metadata handling in the auto-detect
    path would go undetected without this test.
    """
    import json
    import urllib.request

    import chromadb
    from chromadb.config import Settings

    from gptme_rag.embeddings import OpenRouterEmbedding

    persist_dir = tmp_path / "index"
    persist_dir.mkdir()

    # Simulate a collection indexed with OpenRouter (3072-dim vectors).
    settings = Settings(allow_reset=True, is_persistent=True, anonymized_telemetry=False)
    client = chromadb.PersistentClient(path=str(persist_dir), settings=settings)
    stored_model = "openai/text-embedding-3-large"
    col = client.create_collection(
        name="default",
        metadata={
            "hnsw:space": "cosine",
            "embedding_model": stored_model,
            "embedding_backend": "openrouter",
        },
    )
    col.add(
        ids=["or-doc"],
        embeddings=[[0.1] * 3072],
        documents=["an openrouter-indexed document"],
    )
    assert col.count() == 1
    del client

    # Fake HTTP layer so no real API call is made.
    class _FakeResponse:
        def __init__(self, payload: dict):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

    def fake_urlopen(request: urllib.request.Request, timeout: int):
        payload = json.loads(request.data.decode("utf-8"))  # type: ignore[union-attr]
        return _FakeResponse(
            {
                "data": [
                    {"index": i, "embedding": [0.1] * 3072} for i in range(len(payload["input"]))
                ],
                "usage": {"total_tokens": 10, "cost": 0.001},
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    indexer = Indexer(
        persist_directory=persist_dir,
        enable_persist=True,
        embedding_function="auto",
        collection_name="default",
    )

    # The auto-detect path must have constructed an OpenRouterEmbedding, not fallen
    # back to a local embedder or left embedding_function=None.
    assert isinstance(indexer.embedding_function, OpenRouterEmbedding), (
        f"Expected OpenRouterEmbedding, got {type(indexer.embedding_function)}"
    )

    # The rebind must have used the stored model name, not the default.
    assert indexer.embedding_function.model_name == stored_model  # type: ignore[union-attr]

    # The stored collection is preserved.
    assert indexer.collection.count() == 1

    # search() must succeed (return results) without raising.
    results = indexer.search("test query", n_results=1)
    assert len(results) >= 1, "Expected at least one search result"


def test_auto_mode_modernbert_load_failure_preserves_collection(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """auto mode must preserve a modernbert collection when the model cannot be loaded.

    Regression test for P1 bug: the 'modernbert' auto-detect branch created
    ModernBERTEmbedding without try/except. If the model weights are missing or
    unavailable, the constructor raised and the command (search, status) crashed
    instead of falling back gracefully. The fix wraps the call in try/except,
    sets _stored_model_name, and preserves the existing collection.
    """
    import chromadb
    from chromadb.config import Settings

    persist_dir = tmp_path / "index"
    persist_dir.mkdir()

    # Simulate a collection stored with the modernbert backend.
    settings = Settings(allow_reset=True, is_persistent=True, anonymized_telemetry=False)
    client = chromadb.PersistentClient(path=str(persist_dir), settings=settings)
    col = client.create_collection(
        name="default",
        metadata={
            "hnsw:space": "cosine",
            "embedding_model": "modernbert",
            "embedding_backend": "modernbert",
        },
    )
    col.add(ids=["mb-doc"], embeddings=[[0.1] * 768], documents=["a modernbert doc"])
    assert col.count() == 1
    del client

    # Monkey-patch ModernBERTEmbedding to simulate a load failure.
    # Must be a class (not a bare function) so isinstance() calls in the indexer
    # still work when checking if embedding_function is an instance of this type.
    import gptme_rag.indexing.indexer as indexer_mod

    class _FailingModernBERT:
        def __new__(cls, *args, **kwargs):  # type: ignore[misc]
            raise RuntimeError("simulated model load failure: weights unavailable")

    monkeypatch.setattr(indexer_mod, "ModernBERTEmbedding", _FailingModernBERT)

    # Must not raise — collection should be preserved.
    indexer = Indexer(
        persist_directory=persist_dir,
        enable_persist=True,
        embedding_function="auto",
        collection_name="default",
    )

    # load-failed sentinel state
    assert indexer.embedding_function is None
    assert indexer._stored_model_name == "modernbert"

    # The collection must still contain the original document.
    assert indexer.collection.count() == 1

    # search() should raise a clear RuntimeError, not a ChromaDB dimension error.
    with pytest.raises(RuntimeError, match="modernbert"):
        indexer.search("test query", n_results=1)


def test_search_explicit_embedding_function_does_not_wipe_mismatched_collection(
    tmp_path,
):
    """search must never delete a collection when --embedding-function mismatches the stored model.

    Regression test for P1 bug: the search command passed the user's explicit
    --embedding-function directly to Indexer. When it differed from the stored model,
    Indexer set need_recreate=True and deleted the collection — turning a read-only
    operation into silent data loss. The fix adds allow_recreate=False for the search
    Indexer; on mismatch a warning is logged and the collection is kept unchanged.
    """
    import chromadb
    from chromadb.config import Settings

    persist_dir = tmp_path / "index"
    persist_dir.mkdir()

    # Simulate a collection stored with minilm (384-dim).
    settings = Settings(allow_reset=True, is_persistent=True, anonymized_telemetry=False)
    client = chromadb.PersistentClient(path=str(persist_dir), settings=settings)
    col = client.create_collection(
        name="default",
        metadata={
            "hnsw:space": "cosine",
            "embedding_model": "minilm",
            "embedding_backend": "sentence-transformers",
        },
    )
    col.add(ids=["doc-1"], embeddings=[[0.1] * 384], documents=["a minilm doc"])
    assert col.count() == 1
    del client

    # Requesting 'modernbert' (768-dim) with allow_recreate=False must NOT destroy
    # the existing minilm collection.
    indexer = Indexer(
        persist_directory=persist_dir,
        enable_persist=True,
        embedding_function="modernbert",
        collection_name="default",
        allow_recreate=False,
    )

    # Collection must still exist and contain the original document.
    assert indexer.collection.count() == 1, (
        "Collection was destroyed by allow_recreate=False indexer — data loss regression"
    )
