import json
import sqlite3
import urllib.request
from pathlib import Path

import pytest

from gptme_rag.embeddings import OpenRouterEmbedding


class _FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_openrouter_embedding_batches_and_caches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    calls: list[list[str]] = []

    def fake_urlopen(request: urllib.request.Request, timeout: int):
        assert timeout == 180
        assert request.headers["Authorization"] == "Bearer test-key"
        payload = json.loads(request.data.decode("utf-8"))  # type: ignore[union-attr]
        calls.append(payload["input"])
        return _FakeResponse(
            {
                "data": [
                    {"index": index, "embedding": [3.0, 4.0]}
                    for index, _text in enumerate(payload["input"])
                ],
                "usage": {"total_tokens": 7, "cost": 0.001},
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    embedding = OpenRouterEmbedding(
        api_key="test-key",
        cache_path=tmp_path / "embeddings.sqlite",
        max_inputs_per_request=1,
    )

    vectors = embedding(["alpha", "beta"])

    assert calls == [["alpha"], ["beta"]]
    for vector in vectors:
        assert list(vector) == pytest.approx([0.6, 0.8])
    assert embedding.stats["requests"] == 2
    assert embedding.stats["api_texts"] == 2

    cached_vectors = embedding(["alpha", "beta"])

    for vector in cached_vectors:
        assert list(vector) == pytest.approx([0.6, 0.8])
    assert calls == [["alpha"], ["beta"]]
    assert embedding.stats["cached_texts"] == 2


def test_openrouter_embedding_degrades_when_cache_read_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    calls: list[list[str]] = []

    def fake_urlopen(request: urllib.request.Request, timeout: int):
        payload = json.loads(request.data.decode("utf-8"))  # type: ignore[union-attr]
        calls.append(payload["input"])
        return _FakeResponse(
            {
                "data": [
                    {"index": index, "embedding": [3.0, 4.0]}
                    for index, _text in enumerate(payload["input"])
                ],
                "usage": {},
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    embedding = OpenRouterEmbedding(api_key="test-key", cache_path=tmp_path / "embeddings.sqlite")

    def broken_get_many(_model, _hashes):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(embedding.cache, "get_many", broken_get_many)

    vectors = embedding(["alpha"])

    assert calls == [["alpha"]]
    assert list(vectors[0]) == pytest.approx([0.6, 0.8])


def test_openrouter_embedding_degrades_when_cache_write_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    calls: list[list[str]] = []

    def fake_urlopen(request: urllib.request.Request, timeout: int):
        payload = json.loads(request.data.decode("utf-8"))  # type: ignore[union-attr]
        calls.append(payload["input"])
        return _FakeResponse(
            {
                "data": [
                    {"index": index, "embedding": [3.0, 4.0]}
                    for index, _text in enumerate(payload["input"])
                ],
                "usage": {},
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    embedding = OpenRouterEmbedding(api_key="test-key", cache_path=tmp_path / "embeddings.sqlite")

    def broken_put_many(_model, _items):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(embedding.cache, "put_many", broken_put_many)

    vectors = embedding(["alpha"])

    assert calls == [["alpha"]]
    assert list(vectors[0]) == pytest.approx([0.6, 0.8])


def test_openrouter_embedding_requires_api_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY_EVAL", raising=False)

    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        OpenRouterEmbedding(cache_path=tmp_path / "embeddings.sqlite")


def test_openrouter_embedding_count_mismatch_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """_embed_batch must raise RuntimeError if the API returns fewer embeddings than inputs.

    Regression test for P1 bug: the zip() at the call site truncated silently
    when len(rows) < len(texts), leaving some cache keys unpopulated and causing
    a KeyError on the final list comprehension with no clear error message.
    Fix: validate len(rows) == len(texts) before returning from _embed_batch.
    """

    def fake_urlopen(request: urllib.request.Request, timeout: int):
        # Return only one embedding even though two texts were sent
        return _FakeResponse(
            {
                "data": [{"index": 0, "embedding": [1.0, 0.0]}],
                "usage": {},
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    embedding = OpenRouterEmbedding(
        api_key="test-key",
        cache_path=tmp_path / "embeddings.sqlite",
    )

    with pytest.raises(RuntimeError, match="returned 1 vectors for 2 input texts"):
        embedding(["first text", "second text"])


def test_openrouter_embedding_api_level_error_not_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Application-level errors in the API response must not be retried.

    Regression test for P1 bug: a 200 response with an 'error' field (e.g., invalid
    model name, quota exceeded) raised RuntimeError inside the try block, which was
    caught by 'except Exception' and retried up to 5 more times with exponential
    backoff — adding unnecessary delay for permanent failures.
    Fix: 'except RuntimeError: raise' short-circuits the retry loop.
    """
    call_count = 0

    def fake_urlopen(request: urllib.request.Request, timeout: int):
        nonlocal call_count
        call_count += 1
        return _FakeResponse({"error": {"message": "model not found", "code": 404}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    embedding = OpenRouterEmbedding(
        api_key="test-key",
        cache_path=tmp_path / "embeddings.sqlite",
    )

    with pytest.raises(RuntimeError, match="OpenRouter embeddings error"):
        embedding(["test text"])

    # Must fail immediately (1 attempt), not after 6 retries
    assert call_count == 1, f"Expected 1 attempt, got {call_count} (permanent error was retried)"


def test_openrouter_embedding_raises_on_oversized_text(tmp_path: Path):
    """_make_batches must raise ValueError with a descriptive message for texts that
    exceed max_tokens_per_request, rather than silently sending them to the API where
    an HTTP 400 would produce a generic RuntimeError."""
    embedding = OpenRouterEmbedding(
        api_key="test-key",
        cache_path=tmp_path / "embeddings.sqlite",
        max_tokens_per_request=10,
    )
    oversized = "x" * 100  # ~33 approx tokens, well above max of 10

    with pytest.raises(ValueError, match="too large"):
        embedding([oversized])


def test_openrouter_embedding_non_contiguous_indices_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """_embed_batch must raise RuntimeError if the API returns non-contiguous indices.

    A response with correct count but duplicated/out-of-range indices (e.g., [0, 0]
    instead of [0, 1]) would silently assign embeddings to the wrong texts after
    sorting. Validate that [r['index'] for r in rows] == list(range(len(texts))).
    """

    def fake_urlopen(request: urllib.request.Request, timeout: int):
        # Return two embeddings but with duplicate index 0 instead of [0, 1]
        return _FakeResponse(
            {
                "data": [
                    {"index": 0, "embedding": [1.0, 0.0]},
                    {"index": 0, "embedding": [0.0, 1.0]},  # duplicate index — malformed
                ],
                "usage": {},
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    embedding = OpenRouterEmbedding(
        api_key="test-key",
        cache_path=tmp_path / "embeddings.sqlite",
    )

    with pytest.raises(RuntimeError, match="non-contiguous indices"):
        embedding(["first text", "second text"])


def test_sqlite_cache_prunes_oldest_entries_when_over_max_rows(tmp_path: Path):
    """_SQLiteEmbeddingCache must not grow without bound: prune oldest entries past max_rows."""
    from gptme_rag.embeddings import _SQLiteEmbeddingCache

    cache_path = tmp_path / "cache.sqlite"
    max_rows = 5
    cache = _SQLiteEmbeddingCache(path=cache_path, max_rows=max_rows)

    # Insert max_rows entries, then one more batch that triggers pruning.
    model = "test-model"
    for i in range(max_rows):
        h = _SQLiteEmbeddingCache.content_hash(f"doc-{i}")
        cache.put_many(model, [(h, [float(i)])])

    # Count should be exactly max_rows now.
    row_count = cache.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    assert row_count == max_rows, f"Expected {max_rows} rows, got {row_count}"

    # Insert one more — this triggers a prune.
    h_extra = _SQLiteEmbeddingCache.content_hash("doc-extra")
    cache.put_many(model, [(h_extra, [99.0])])

    row_count = cache.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    assert (
        row_count <= max_rows
    ), f"Cache should not exceed max_rows={max_rows} after prune, got {row_count}"
    # The freshly-inserted entry must survive (it was just written).
    hit = cache.get_many(model, [h_extra])
    assert h_extra in hit, "Freshly inserted entry was pruned — recency ordering is wrong"


def test_sqlite_cache_batch_insert_preserves_all_new_items(tmp_path: Path):
    """put_many must never prune items from the batch it is currently inserting.

    Regression: with a post-insert prune and all batch rows sharing the same
    ``accessed_at`` timestamp (executemany evaluates the SQL expression once),
    ORDER BY accessed_at ASC has no tiebreaker and can arbitrarily delete
    freshly-inserted entries.
    """
    from gptme_rag.embeddings import _SQLiteEmbeddingCache

    cache_path = tmp_path / "cache.sqlite"
    max_rows = 5
    cache = _SQLiteEmbeddingCache(path=cache_path, max_rows=max_rows)

    model = "test-model"
    # Fill the cache to capacity with old entries.
    for i in range(max_rows):
        h = _SQLiteEmbeddingCache.content_hash(f"old-{i}")
        cache.put_many(model, [(h, [float(i)])])

    # Insert a batch of 3 new items — all share the same accessed_at timestamp.
    # With a post-insert prune and no tiebreaker, any of these could be evicted.
    batch = [(_SQLiteEmbeddingCache.content_hash(f"new-{i}"), [float(100 + i)]) for i in range(3)]
    batch_hashes = {h for h, _ in batch}
    cache.put_many(model, batch)

    row_count = cache.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    assert row_count <= max_rows, f"Cache exceeded max_rows={max_rows}: {row_count}"

    hits = cache.get_many(model, list(batch_hashes))
    missing = batch_hashes - set(hits.keys())
    assert not missing, f"Freshly inserted items were pruned: {missing}"


def test_sqlite_cache_oversized_batch_capped_to_max_rows(tmp_path: Path):
    """A single put_many batch larger than max_rows must not exceed the cap."""
    from gptme_rag.embeddings import _SQLiteEmbeddingCache

    cache_path = tmp_path / "cache.sqlite"
    max_rows = 5
    cache = _SQLiteEmbeddingCache(path=cache_path, max_rows=max_rows)

    model = "test-model"
    # 2*max_rows items in one shot — previously, all got the same timestamp so
    # the prune arbitrarily deleted half of the just-inserted rows.
    batch = [
        (_SQLiteEmbeddingCache.content_hash(f"doc-{i}"), [float(i)]) for i in range(max_rows * 2)
    ]
    cache.put_many(model, batch)

    row_count = cache.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    assert row_count == max_rows, f"Expected exactly {max_rows} rows, got {row_count}"
