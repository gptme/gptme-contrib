"""Caller floor for the reservation-402 affordable-token retry."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest
from gptme_review import core

AFFORDABLE = 857


def _afford_402(requested: int) -> urllib.error.HTTPError:
    body = json.dumps(
        {
            "error": {
                "message": (
                    f"This request requires more credits, or fewer max_tokens. "
                    f"You requested up to {requested} tokens, but can only "
                    f"afford {AFFORDABLE}."
                ),
                "code": 402,
            }
        }
    )
    return urllib.error.HTTPError(
        "https://openrouter.ai/api/v1/chat/completions",
        402,
        "Payment Required",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body.encode()),
    )


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def read1(self, size: int | None = -1) -> bytes:
        return self.read(size)


def _truncated_answer() -> bytes:
    return json.dumps(
        {
            "id": "gen-truncated",
            "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
            "usage": {"completion_tokens": AFFORDABLE},
        }
    ).encode()


@pytest.fixture
def requested(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    def fake_urlopen(req, timeout):  # noqa: ARG001
        max_tokens = json.loads(req.data.decode())["max_tokens"]
        calls.append(max_tokens)
        if max_tokens > AFFORDABLE:
            raise _afford_402(max_tokens)
        return _FakeResponse(_truncated_answer())

    monkeypatch.setattr(core.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_default_floor_still_retries_with_affordable_budget(requested: list[int]):
    data = core._request(
        {"model": "m", "max_tokens": 4000, "messages": []}, api_key="k", timeout=5
    )
    assert requested == [4000, AFFORDABLE]
    assert data["id"] == "gen-truncated"


def test_caller_floor_raises_402_instead_of_shrinking(requested: list[int]):
    with pytest.raises(core.ReviewError, match="HTTP 402"):
        core._request(
            {"model": "m", "max_tokens": 4000, "messages": []},
            api_key="k",
            timeout=5,
            min_affordable_tokens=4000,
        )
    assert requested == [4000]


def test_caller_floor_below_afford_count_still_retries(requested: list[int]):
    core._request(
        {"model": "m", "max_tokens": 4000, "messages": []},
        api_key="k",
        timeout=5,
        min_affordable_tokens=500,
    )
    assert requested == [4000, AFFORDABLE]


def test_completion_threads_floor_to_the_wire(requested: list[int]):
    with pytest.raises(core.ReviewError, match="HTTP 402"):
        core.openrouter_completion(
            "judge this",
            model="deepseek/deepseek-v4-flash-0731",
            api_key="k",
            timeout=5,
            max_tokens=4000,
            min_affordable_tokens=4000,
        )
    assert requested == [4000]
