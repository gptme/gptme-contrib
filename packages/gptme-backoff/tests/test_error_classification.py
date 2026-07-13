"""Tests for the Phase 2 error-classification retry layer."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from gptme_backoff.error_classification import (
    DEFAULT_STRATEGY_CONFIGS,
    ErrorClassifier,
    RetryStrategy,
    StrategyConfig,
    retry_classified,
)

# --- fake exceptions mimicking common client libraries ---------------------


class HTTPError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class RequestsStyleError(Exception):
    """requests/httpx-style error carrying a .response.status_code."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"response {status_code}")
        self.response = _Response(status_code)


# --- StrategyConfig.compute_wait -------------------------------------------


def test_compute_wait_exponential_without_jitter() -> None:
    cfg = StrategyConfig(base_wait=1.0, multiplier=2.0, max_wait=100.0, jitter=False)
    assert cfg.compute_wait(1) == 1.0
    assert cfg.compute_wait(2) == 2.0
    assert cfg.compute_wait(3) == 4.0


def test_compute_wait_respects_max_wait() -> None:
    cfg = StrategyConfig(base_wait=10.0, multiplier=10.0, max_wait=15.0, jitter=False)
    assert cfg.compute_wait(1) == 10.0
    assert cfg.compute_wait(2) == 15.0  # capped


def test_compute_wait_additive_jitter_range() -> None:
    cfg = StrategyConfig(base_wait=4.0, multiplier=1.0, max_wait=100.0, jitter=True)
    for _ in range(50):
        w = cfg.compute_wait(1)
        assert 4.0 <= w < 8.0  # additive jitter: [wait, 2*wait), not AWS full jitter


def test_compute_wait_rejects_bad_attempt() -> None:
    with pytest.raises(ValueError):
        StrategyConfig().compute_wait(0)


# --- ErrorClassifier --------------------------------------------------------


def test_classify_rate_limit_by_status_code() -> None:
    c = ErrorClassifier.default()
    assert c.classify(HTTPError(429)) is RetryStrategy.RATE_LIMIT


def test_classify_auth_and_client_errors_fail_fast() -> None:
    c = ErrorClassifier.default()
    for code in (400, 401, 403, 404, 422):
        assert c.classify(HTTPError(code)) is RetryStrategy.AUTH


def test_classify_server_error_is_transient() -> None:
    c = ErrorClassifier.default()
    assert c.classify(HTTPError(500)) is RetryStrategy.TRANSIENT
    assert c.classify(HTTPError(503)) is RetryStrategy.TRANSIENT


def test_classify_retryable_4xx_is_transient() -> None:
    # 408 Request Timeout and 425 Too Early are client-class but transient,
    # so they must be retried rather than failing fast like 401/403/404.
    c = ErrorClassifier.default()
    assert c.classify(HTTPError(408)) is RetryStrategy.TRANSIENT
    assert c.classify(HTTPError(425)) is RetryStrategy.TRANSIENT
    # Other 4xx still fail fast.
    assert c.classify(HTTPError(400)) is RetryStrategy.AUTH


def test_classify_uses_response_status_code() -> None:
    c = ErrorClassifier.default()
    assert c.classify(RequestsStyleError(429)) is RetryStrategy.RATE_LIMIT
    assert c.classify(RequestsStyleError(401)) is RetryStrategy.AUTH


def test_classify_builtin_connection_errors_transient() -> None:
    c = ErrorClassifier.default()
    assert c.classify(ConnectionError("reset")) is RetryStrategy.TRANSIENT
    assert c.classify(TimeoutError("slow")) is RetryStrategy.TRANSIENT


def test_classify_unknown_falls_back() -> None:
    c = ErrorClassifier.default()
    assert c.classify(ValueError("???")) is RetryStrategy.UNKNOWN


def test_register_custom_type_rule() -> None:
    class MyConsistencyError(Exception):
        pass

    c = ErrorClassifier.default()
    c.register(RetryStrategy.CONSISTENCY, exc_types=[MyConsistencyError])
    assert c.classify(MyConsistencyError()) is RetryStrategy.CONSISTENCY


def test_register_prepend_shadows_broad_rule() -> None:
    c = ErrorClassifier.default()
    # By default a 503 is TRANSIENT; override 503 specifically to CONSISTENCY.
    c.register(
        RetryStrategy.CONSISTENCY,
        predicate=lambda e: getattr(e, "status_code", None) == 503,
    )
    assert c.classify(HTTPError(503)) is RetryStrategy.CONSISTENCY
    assert c.classify(HTTPError(500)) is RetryStrategy.TRANSIENT


def test_register_requires_matcher() -> None:
    c = ErrorClassifier()
    with pytest.raises(ValueError):
        c.register(RetryStrategy.AUTH)


def test_broken_predicate_does_not_crash_classify() -> None:
    c = ErrorClassifier()

    def boom(_exc: BaseException) -> bool:
        raise RuntimeError("predicate bug")

    c.register(RetryStrategy.TRANSIENT, predicate=boom)
    assert c.classify(ValueError()) is RetryStrategy.UNKNOWN  # falls through


# --- retry_classified -------------------------------------------------------


def _recording_sleep() -> tuple[list[float], Callable[[float], None]]:
    waits: list[float] = []
    return waits, waits.append


def test_retry_classified_success_no_retry() -> None:
    waits, sleep = _recording_sleep()
    calls = {"n": 0}

    @retry_classified(sleep=sleep)
    def ok() -> str:
        calls["n"] += 1
        return "done"

    assert ok() == "done"
    assert calls["n"] == 1
    assert waits == []


def test_retry_classified_rate_limit_retries_then_succeeds() -> None:
    waits, sleep = _recording_sleep()
    calls = {"n": 0}

    @retry_classified(sleep=sleep)
    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise HTTPError(429)
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3
    assert len(waits) == 2  # two backoffs before the 3rd success


def test_retry_classified_auth_fails_fast() -> None:
    waits, sleep = _recording_sleep()
    calls = {"n": 0}

    @retry_classified(sleep=sleep)
    def denied() -> None:
        calls["n"] += 1
        raise HTTPError(401)

    with pytest.raises(HTTPError):
        denied()
    assert calls["n"] == 1  # no retry
    assert waits == []


def test_retry_classified_exhausts_attempts_and_reraises() -> None:
    waits, sleep = _recording_sleep()
    calls = {"n": 0}
    max_attempts = DEFAULT_STRATEGY_CONFIGS[RetryStrategy.RATE_LIMIT].max_attempts

    @retry_classified(sleep=sleep)
    def always_429() -> None:
        calls["n"] += 1
        raise HTTPError(429)

    with pytest.raises(HTTPError):
        always_429()
    assert calls["n"] == max_attempts
    assert len(waits) == max_attempts - 1


def test_retry_classified_success_criterion() -> None:
    """Task success criterion: 429 retries with backoff; 401 fails immediately."""
    waits, sleep = _recording_sleep()

    @retry_classified(sleep=sleep)
    def on_429() -> str:
        if not waits:
            raise HTTPError(429)
        return "recovered"

    assert on_429() == "recovered"
    assert len(waits) == 1 and waits[0] > 0  # jittered exponential backoff happened

    auth_waits, auth_sleep = _recording_sleep()

    @retry_classified(sleep=auth_sleep)
    def on_401() -> None:
        raise HTTPError(401)

    with pytest.raises(HTTPError):
        on_401()
    assert auth_waits == []  # immediate, no retry


def test_retry_classified_on_retry_callback() -> None:
    waits, sleep = _recording_sleep()
    events: list[tuple[RetryStrategy, int]] = []

    def on_retry(
        exc: BaseException,
        strategy: RetryStrategy,
        next_attempt: int,
        wait: float,
    ) -> None:
        events.append((strategy, next_attempt))

    @retry_classified(sleep=sleep, on_retry=on_retry)
    def flaky() -> str:
        if len(events) < 2:
            raise HTTPError(429)
        return "ok"

    assert flaky() == "ok"
    assert events == [
        (RetryStrategy.RATE_LIMIT, 2),
        (RetryStrategy.RATE_LIMIT, 3),
    ]


def test_retry_classified_custom_configs_override() -> None:
    waits, sleep = _recording_sleep()
    calls = {"n": 0}
    configs = {RetryStrategy.UNKNOWN: StrategyConfig(max_attempts=5, jitter=False)}

    @retry_classified(sleep=sleep, configs=configs)
    def boom() -> None:
        calls["n"] += 1
        raise ValueError("???")  # classified UNKNOWN

    with pytest.raises(ValueError):
        boom()
    assert calls["n"] == 5


# --- async retry_classified --------------------------------------------------


@pytest.mark.asyncio
async def test_retry_classified_async_success_no_retry() -> None:
    """async functions work and are actually awaited."""
    calls = {"n": 0}

    @retry_classified(sleep=lambda _: None)
    async def ok() -> str:
        calls["n"] += 1
        return "done"

    result = await ok()
    assert result == "done"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_retry_classified_async_rate_limit_retries() -> None:
    """Errors raised inside the coroutine body are retried with asyncio.sleep."""
    calls = {"n": 0}
    configs = {
        RetryStrategy.RATE_LIMIT: StrategyConfig(
            max_attempts=3, base_wait=0.0, multiplier=1.0, max_wait=0.0, jitter=False
        )
    }

    @retry_classified(configs=configs)
    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise HTTPError(429)
        return "ok"

    result = await flaky()
    assert result == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_retry_classified_async_auth_fails_fast() -> None:
    """AUTH errors are not retried in async mode."""
    calls = {"n": 0}

    @retry_classified()
    async def denied() -> None:
        calls["n"] += 1
        raise HTTPError(401)

    with pytest.raises(HTTPError):
        await denied()
    assert calls["n"] == 1
