"""Content-hash embedding cache for local sentence-transformers backends.

Change detection in ``gptme-rag index`` is per file: one appended line
re-embeds every chunk of that file even though all but the tail chunk are
byte-identical to what the index already holds. On CPU that re-embed is the
whole cost of an incremental run, so unchanged chunks must be cache hits.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

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


def test_cached_encode_is_keyed_by_model(tmp_path: Path) -> None:
    cache = emb._SQLiteEmbeddingCache(tmp_path / "cache.sqlite")
    encoder = _CountingEncoder()
    emb.cached_encode(["alpha"], encode=encoder, model_name="m1", cache=cache)
    emb.cached_encode(["alpha"], encode=encoder, model_name="m2", cache=cache)
    assert len(encoder.calls) == 2


def test_cached_encode_without_cache_and_empty_input() -> None:
    encoder = _CountingEncoder()
    assert emb.cached_encode([], encode=encoder, model_name="m", cache=None) == []
    assert emb.cached_encode(["x"], encode=encoder, model_name="m", cache=None) == [[1.0, 1.0]]
    assert encoder.calls == [["x"]]


def test_cached_encode_degrades_when_cache_io_fails(tmp_path: Path) -> None:
    class BrokenCache(emb._SQLiteEmbeddingCache):
        def get_many(self, model, hashes):  # type: ignore[override]
            raise sqlite3.OperationalError("database is locked")

        def put_many(self, model, items):  # type: ignore[override]
            raise sqlite3.OperationalError("database is locked")

    cache = BrokenCache(tmp_path / "cache.sqlite")
    encoder = _CountingEncoder()
    out = emb.cached_encode(["alpha", "beta"], encode=encoder, model_name="m", cache=cache)
    assert out == [[5.0, 1.0], [4.0, 1.0]]
    assert encoder.calls == [["alpha", "beta"]]


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
        _FakeSentenceTransformer.instances.append(self)

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
