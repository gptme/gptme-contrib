"""retry_classified(sleep=...) is silently ignored for async functions."""

import asyncio

import pytest
from gptme_backoff.error_classification import retry_classified


class HTTPError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


@pytest.mark.asyncio
async def test_async_path_uses_injected_sleep(monkeypatch):
    real_sleeps = []

    async def fake_asyncio_sleep(s):
        real_sleeps.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_asyncio_sleep)
    injected = []
    calls = {"n": 0}

    @retry_classified(sleep=injected.append)
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise HTTPError(429)
        return "ok"

    assert await flaky() == "ok"
    # the caller-injected sleep should have received the backoff; instead the
    # real asyncio.sleep(~2-4s) ran and the injected hook was never called
    assert len(injected) == 1, (injected, real_sleeps)
