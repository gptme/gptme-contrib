"""Content-hash embedding cache for local sentence-transformers backends.

Change detection in ``gptme-rag index`` is per file: one appended line
re-embeds every chunk of that file even though all but the tail chunk are
byte-identical to what the index already holds. On CPU that re-embed is the
whole cost of an incremental run, so unchanged chunks must be cache hits.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from gptme_rag import embeddings as emb


class _CountingEncoder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(text)), 1.0] for text in texts]


def test_cached_encode_embeds_only_misses_and_dedupes(tmp_path: Path) -> None:
    cache = emb._SQLiteEmbeddingCache(tmp_path / "cache.sqlite")
    encoder = _CountingEncoder()
    stats: dict[str, int] = {}

    first = emb.cached_encode(
        ["alpha", "beta", "alpha"], encode=encoder, model_name="m", cache=cache, stats=stats
    )
    assert first == [[5.0, 1.0], [4.0, 1.0], [5.0, 1.0]]
    assert encoder.calls == [["alpha", "beta"]]  # in-call duplicate embedded once
    assert stats == {"cached_texts": 0, "embedded_texts": 2}

    second = emb.cached_encode(
        ["beta", "gamma", "alpha"], encode=encoder, model_name="m", cache=cache, stats=stats
    )
    assert second == [[4.0, 1.0], [5.0, 1.0], [5.0, 1.0]]
    assert encoder.calls[-1] == ["gamma"]  # only the new chunk hit the model
    assert stats == {"cached_texts": 2, "embedded_texts": 3}


def test_cached_encode_is_keyed_by_model_revision(tmp_path: Path) -> None:
    cache = emb._SQLiteEmbeddingCache(tmp_path / "cache.sqlite")
    encoder = _CountingEncoder()
    emb.cached_encode(["alpha"], encode=encoder, model_name="m@revision-1", cache=cache)
    emb.cached_encode(["alpha"], encode=encoder, model_name="m@revision-2", cache=cache)
    assert len(encoder.calls) == 2


def test_sentence_transformer_cache_key_includes_resolved_revision() -> None:
    class Model:
        def modules(self):
            return [SimpleNamespace(config=SimpleNamespace(_commit_hash="abc123"))]

    assert emb._sentence_transformer_cache_key("org/model", Model()) == "org/model@abc123"


def test_sentence_transformer_cache_key_requires_stable_revision() -> None:
    class Model:
        def modules(self):
            return [SimpleNamespace(config=SimpleNamespace(_commit_hash=None))]

    assert emb._sentence_transformer_cache_key("org/model", Model()) is None


def test_cache_stats_update_under_lock(tmp_path: Path) -> None:
    class RecordingLock:
        entered = False

        def __enter__(self):
            self.entered = True

        def __exit__(self, *_args):
            return None

    cache = emb._SQLiteEmbeddingCache(tmp_path / "cache.sqlite")
    stats: dict[str, int] = {}
    lock = RecordingLock()
    emb.cached_encode(
        ["alpha"],
        encode=_CountingEncoder(),
        model_name="m",
        cache=cache,
        stats=stats,
        stats_lock=lock,  # type: ignore[arg-type]
    )
    assert lock.entered
    assert stats == {"cached_texts": 0, "embedded_texts": 1}


def test_cached_encode_without_cache_dedupes_and_handles_empty_input() -> None:
    encoder = _CountingEncoder()
    assert emb.cached_encode([], encode=encoder, model_name="m", cache=None) == []
    assert emb.cached_encode(["x", "y", "x"], encode=encoder, model_name="m", cache=None) == [
        [1.0, 1.0],
        [1.0, 1.0],
        [1.0, 1.0],
    ]
    assert encoder.calls == [["x", "y"]]


@pytest.mark.parametrize(
    "error", [sqlite3.OperationalError("database is locked"), ValueError("bad JSON")]
)
def test_cached_encode_degrades_when_cache_read_fails(tmp_path: Path, error: Exception) -> None:
    class BrokenCache(emb._SQLiteEmbeddingCache):
        def get_many(self, model, hashes):  # type: ignore[override]
            raise error

    cache = BrokenCache(tmp_path / "cache.sqlite")
    encoder = _CountingEncoder()
    out = emb.cached_encode(["alpha", "beta", "alpha"], encode=encoder, model_name="m", cache=cache)
    assert out == [[5.0, 1.0], [4.0, 1.0], [5.0, 1.0]]
    assert encoder.calls == [["alpha", "beta"]]


def test_cache_ignores_and_replaces_invalid_vectors(tmp_path: Path) -> None:
    cache = emb._SQLiteEmbeddingCache(tmp_path / "cache.sqlite")
    content_hash = cache.content_hash("alpha")
    cache.conn.execute(
        "INSERT INTO embeddings (model, content_hash, embedding_json) VALUES (?, ?, ?)",
        ("m", content_hash, "null"),
    )
    cache.conn.commit()
    encoder = _CountingEncoder()

    assert emb.cached_encode(["alpha"], encode=encoder, model_name="m", cache=cache) == [[5.0, 1.0]]
    assert cache.get_many("m", [content_hash]) == {content_hash: [5.0, 1.0]}


def test_cache_ignores_and_replaces_malformed_json(tmp_path: Path) -> None:
    cache = emb._SQLiteEmbeddingCache(tmp_path / "cache.sqlite")
    content_hash = cache.content_hash("alpha")
    cache.conn.execute(
        "INSERT INTO embeddings (model, content_hash, embedding_json) VALUES (?, ?, ?)",
        ("m", content_hash, "[not-json"),
    )
    cache.conn.commit()

    encoder = _CountingEncoder()
    assert emb.cached_encode(["alpha"], encode=encoder, model_name="m", cache=cache) == [[5.0, 1.0]]


def test_cache_hits_refresh_access_time(tmp_path: Path) -> None:
    cache = emb._SQLiteEmbeddingCache(tmp_path / "cache.sqlite")
    content_hash = cache.content_hash("alpha")
    cache.put_many("m", [(content_hash, [1.0])])
    cache.conn.execute(
        "UPDATE embeddings SET accessed_at = '2000-01-01T00:00:00.000Z' WHERE model = 'm'"
    )
    cache.conn.commit()

    assert cache.get_many("m", [content_hash]) == {content_hash: [1.0]}
    refreshed = cache.conn.execute(
        "SELECT accessed_at FROM embeddings WHERE model = 'm' AND content_hash = ?",
        (content_hash,),
    ).fetchone()[0]
    assert refreshed > "2000-01-01T00:00:00.000Z"


def test_cache_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    path.touch(mode=0o644)
    emb._SQLiteEmbeddingCache(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_open_local_embedding_cache_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = tmp_path / "explicit.sqlite"
    assert emb._open_local_embedding_cache(explicit) is not None
    assert explicit.exists()

    for value in ("off", "0", "false"):
        monkeypatch.setenv(emb.LOCAL_EMBEDDING_CACHE_ENV, value)
        assert emb._open_local_embedding_cache(None) is None

    env_path = tmp_path / "from-env.sqlite"
    monkeypatch.setenv(emb.LOCAL_EMBEDDING_CACHE_ENV, str(env_path))
    assert emb._open_local_embedding_cache(None) is not None
    assert env_path.exists()

    monkeypatch.delenv(emb.LOCAL_EMBEDDING_CACHE_ENV)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert emb._open_local_embedding_cache(None) is not None
    assert (tmp_path / "xdg" / "gptme-rag" / "local-embeddings.sqlite").exists()

    for empty in ("", "   "):
        monkeypatch.setenv("XDG_CACHE_HOME", empty)
        assert emb._default_local_cache_path() == (
            Path.home() / ".cache" / "gptme-rag" / "local-embeddings.sqlite"
        )

    # Unopenable path degrades to "no cache", never to an exception.
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    assert emb._open_local_embedding_cache(blocker / "cache.sqlite") is None


def _as_lists(vectors) -> list[list[float]]:
    """chromadb's EmbeddingFunction wrapper normalises outputs to numpy arrays."""
    return [[float(x) for x in vector] for vector in vectors]


class _FakeSentenceTransformer:
    """Stands in for the ~80MB model: records what actually reaches encode()."""

    instances: list[_FakeSentenceTransformer] = []

    def __init__(self, model_name: str, device: str = "cpu") -> None:
        self.model_name = model_name
        self.encoded: list[list[str]] = []
        self._revision = "fake-revision"
        _FakeSentenceTransformer.instances.append(self)

    def modules(self):
        return [SimpleNamespace(config=SimpleNamespace(_commit_hash=self._revision))]

    def encode(self, texts, batch_size, convert_to_numpy, normalize_embeddings):
        self.encoded.append(list(texts))
        return np.array([[float(len(t)), 0.5] for t in texts])


@pytest.mark.parametrize(
    "factory",
    [
        lambda path: emb.ModernBERTEmbedding(cache_path=path),
        lambda path: emb.GenericSentenceTransformerEmbedding("all-MiniLM-L6-v2", cache_path=path),
    ],
    ids=["modernbert", "generic"],
)
def test_local_embedding_functions_reuse_cached_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, factory
) -> None:
    monkeypatch.setattr(emb, "SentenceTransformer", _FakeSentenceTransformer)
    _FakeSentenceTransformer.instances.clear()

    fn = factory(tmp_path / "cache.sqlite")
    model = _FakeSentenceTransformer.instances[-1]

    # First index pass: a two-chunk file.
    assert _as_lists(fn(["chunk one", "chunk two"])) == [[9.0, 0.5], [9.0, 0.5]]
    assert model.encoded == [["chunk one", "chunk two"]]

    # The file gets a line appended: the file hash changes, every chunk is
    # re-submitted, but only the changed tail chunk reaches the model.
    assert _as_lists(fn(["chunk one", "chunk two + more"])) == [[9.0, 0.5], [16.0, 0.5]]
    assert model.encoded[-1] == ["chunk two + more"]
    assert fn.stats == {"cached_texts": 1, "embedded_texts": 3}

    # A fresh process sharing the cache file sees the same hits.
    again = factory(tmp_path / "cache.sqlite")
    assert _as_lists(again(["chunk one"])) == [[9.0, 0.5]]
    assert _FakeSentenceTransformer.instances[-1].encoded == []


def test_local_embedding_cache_disabled_by_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(emb, "SentenceTransformer", _FakeSentenceTransformer)
    monkeypatch.setenv(emb.LOCAL_EMBEDDING_CACHE_ENV, "off")
    fn = emb.ModernBERTEmbedding()
    assert fn.cache is None
    fn(["a"])
    fn(["a"])
    assert fn.model.encoded == [["a"], ["a"]]
