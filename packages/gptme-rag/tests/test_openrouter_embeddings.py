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
