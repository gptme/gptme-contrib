import json
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


def test_openrouter_embedding_requires_api_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY_EVAL", raising=False)

    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        OpenRouterEmbedding(cache_path=tmp_path / "embeddings.sqlite")


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
