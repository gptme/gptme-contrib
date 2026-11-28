"""Tests for gptme-backoff retry decorators."""

import pytest
import tenacity
from gptme_backoff import retry_api_call, retry_async, retry_file_op, retry_sync


class _TemporaryError(Exception):
    """Temporary error used in tests."""


class _PermanentError(Exception):
    """Permanent error that should not be retried."""


# ---- Sync retry tests ----


def test_retry_sync_eventually_succeeds():
    call_count = [0]

    @retry_sync(
        stop=tenacity.stop_after_attempt(3),
        wait=tenacity.wait_fixed(0.01),
    )
    def unstable() -> str:
        call_count[0] += 1
        if call_count[0] < 3:
            raise _TemporaryError("not yet")
        return "ok"

    result = unstable()
    assert result == "ok"
    assert call_count[0] == 3


def test_retry_sync_exhausted():
    call_count = [0]

    @retry_sync(
        stop=tenacity.stop_after_attempt(2),
        wait=tenacity.wait_fixed(0.01),
    )
    def always_fails() -> str:
        call_count[0] += 1
        raise _TemporaryError("boom")

    with pytest.raises(_TemporaryError, match="boom"):
        always_fails()
    assert call_count[0] == 2


def test_retry_sync_does_not_retry_on_success():
    call_count = [0]

    @retry_sync(
        stop=tenacity.stop_after_attempt(5),
        wait=tenacity.wait_fixed(0.01),
    )
    def always_ok() -> str:
        call_count[0] += 1
        return "ok"

    result = always_ok()
    assert result == "ok"
    assert call_count[0] == 1


# ---- Async retry tests ----


@pytest.mark.asyncio
async def test_retry_async_eventually_succeeds():
    call_count = [0]

    @retry_async(
        stop=tenacity.stop_after_attempt(3),
        wait=tenacity.wait_fixed(0.01),
    )
    async def unstable() -> str:
        call_count[0] += 1
        if call_count[0] < 3:
            raise _TemporaryError("not yet")
        return "ok"

    result = await unstable()
    assert result == "ok"
    assert call_count[0] == 3


@pytest.mark.asyncio
async def test_retry_async_exhausted():
    call_count = [0]

    @retry_async(
        stop=tenacity.stop_after_attempt(2),
        wait=tenacity.wait_fixed(0.01),
    )
    async def always_fails() -> str:
        call_count[0] += 1
        raise _TemporaryError("boom")

    with pytest.raises(_TemporaryError, match="boom"):
        await always_fails()
    assert call_count[0] == 2


@pytest.mark.asyncio
async def test_retry_async_first_call_ok():
    call_count = [0]

    @retry_async(
        stop=tenacity.stop_after_attempt(3),
        wait=tenacity.wait_fixed(0.01),
    )
    async def always_ok() -> str:
        call_count[0] += 1
        return "ok"

    result = await always_ok()
    assert result == "ok"
    assert call_count[0] == 1


# ---- Preset: API call ----


def test_retry_api_call_preset():
    call_count = [0]

    @retry_api_call(max_attempts=3)
    def unstable() -> str:
        call_count[0] += 1
        if call_count[0] < 3:
            raise _TemporaryError("rate limited")
        return "ok"

    result = unstable()
    assert result == "ok"
    assert call_count[0] == 3


def test_retry_api_call_exhausted():
    call_count = [0]

    @retry_api_call(max_attempts=2)
    def always_fails() -> str:
        call_count[0] += 1
        raise _TemporaryError("server error")

    with pytest.raises(_TemporaryError, match="server error"):
        always_fails()
    assert call_count[0] == 2


@pytest.mark.asyncio
async def test_retry_api_call_retries_async_functions():
    call_count = [0]

    @retry_api_call(max_attempts=3, min_wait=0.01, max_wait=0.01, jitter=False)
    async def unstable() -> str:
        call_count[0] += 1
        if call_count[0] < 3:
            raise _TemporaryError("rate limited")
        return "ok"

    result = await unstable()
    assert result == "ok"
    assert call_count[0] == 3


def test_retry_api_call_timeout_stops_retries():
    """stop_before_delay limits the total retry budget, not per-call execution time."""
    call_count = [0]

    @retry_api_call(max_attempts=10, timeout=0.02)
    def always_fails() -> str:
        call_count[0] += 1
        raise _TemporaryError("transient")

    with pytest.raises(_TemporaryError, match="transient"):
        always_fails()
    # Should stop retrying after ~0.02s, not attempt all 10
    assert (
        call_count[0] < 10
    ), "timeout should stop retries before exhausting max_attempts"


# ---- Preset: File operation ----


def test_retry_file_op_on_oserror():
    call_count = [0]

    @retry_file_op(max_attempts=3)
    def unstable() -> str:
        call_count[0] += 1
        if call_count[0] < 3:
            raise OSError("file locked")
        return "ok"

    result = unstable()
    assert result == "ok"
    assert call_count[0] == 3


def test_retry_file_op_does_not_retry_on_valueerror():
    """File op preset should NOT retry non-OSError exceptions."""
    call_count = [0]

    @retry_file_op(max_attempts=3)
    def fails_with_value_error() -> str:
        call_count[0] += 1
        raise _TemporaryError("not an OSError")

    # Non-OSError should propagate immediately
    with pytest.raises(_TemporaryError):
        fails_with_value_error()
    assert call_count[0] == 1


@pytest.mark.asyncio
async def test_retry_file_op_retries_async_on_oserror():
    """Async file ops are retried on OSError (not swallowed as a coroutine object)."""
    call_count = [0]

    @retry_file_op(max_attempts=3, min_wait=0.01, max_wait=0.01)
    async def unstable() -> str:
        call_count[0] += 1
        if call_count[0] < 3:
            raise OSError("file locked")
        return "ok"

    result = await unstable()
    assert result == "ok"
    assert call_count[0] == 3


# ---- Edge cases ----


def test_reraise_true_is_default():
    """Default retry_sync should re-raise original exception (reraise=True)."""
    call_count = [0]

    @retry_sync(
        stop=tenacity.stop_after_attempt(2),
        wait=tenacity.wait_fixed(0.01),
    )
    def always_fails() -> str:
        call_count[0] += 1
        raise _TemporaryError("boom")

    with pytest.raises(_TemporaryError, match="boom"):
        always_fails()
    assert call_count[0] == 2


def test_kwargs_preserved():
    """retry_kwargs should be passed through to tenacity."""
    call_count = [0]

    @retry_sync(
        stop=tenacity.stop_after_attempt(2),
        wait=tenacity.wait_fixed(0.01),
        retry=tenacity.retry_if_exception_type(_TemporaryError),
    )
    def func() -> str:
        call_count[0] += 1
        raise _PermanentError("not retriable")

    with pytest.raises(_PermanentError):
        func()
    assert call_count[0] == 1  # no retry for non-temporary errors


@pytest.mark.asyncio
async def test_retry_api_call_retries_async_callable_objects():
    """retry_api_call must detect callable objects with async __call__ as async.

    inspect.iscoroutinefunction(obj) returns False for such objects; the fix uses
    _is_coroutine_callable which also checks obj.__call__.  Without the fix, the
    sync tenacity wrapper returns the coroutine object as a "success" result and
    never retries.
    """
    call_count = [0]

    class AsyncFetcher:
        async def __call__(self) -> str:
            call_count[0] += 1
            if call_count[0] < 3:
                raise OSError("transient")
            return "ok"

    fetcher = retry_api_call(max_attempts=5, min_wait=0, max_wait=0)(AsyncFetcher())
    result = await fetcher()
    assert result == "ok"
    assert call_count[0] == 3


@pytest.mark.asyncio
async def test_retry_file_op_retries_async_callable_objects():
    """retry_file_op must route callable objects with async __call__ through retry_async."""
    call_count = [0]

    class AsyncReader:
        async def __call__(self) -> str:
            call_count[0] += 1
            if call_count[0] < 2:
                raise OSError("busy")
            return "data"

    reader = retry_file_op(max_attempts=3, min_wait=0, max_wait=0)(AsyncReader())
    result = await reader()
    assert result == "data"
    assert call_count[0] == 2


# ---- Wait-curve contract (gptme/gptme-contrib#2586) ----
#
# Base wait before retry n (n=1 after the first failure):
#   min(max_wait, min_wait * multiplier ** (n - 1))
# Jitter only adds noise in [0, min_wait] and the final wait is capped again.


@pytest.fixture
def recorded_sleeps(monkeypatch):
    """Record sync and async sleeps instead of performing them."""
    import asyncio
    import time

    waits: list[float] = []

    async def _fake_async_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(time, "sleep", waits.append)
    monkeypatch.setattr(asyncio, "sleep", _fake_async_sleep)
    return waits


@pytest.fixture
def jitter_noise(monkeypatch):
    """Control the random noise tenacity's jitter wait adds (default: zero)."""
    import random

    state = {"noise": 0.0}

    def _uniform(low: float, high: float) -> float:
        return min(high, low + state["noise"])

    monkeypatch.setattr(random, "uniform", _uniform)
    return state


def _exhaust_sync(decorator, exc: type[BaseException]) -> int:
    calls = 0

    @decorator
    def always_fails():
        nonlocal calls
        calls += 1
        raise exc("synthetic")

    with pytest.raises(exc):
        always_fails()
    return calls


async def _exhaust_async(decorator, exc: type[BaseException]) -> int:
    calls = 0

    @decorator
    async def always_fails():
        nonlocal calls
        calls += 1
        raise exc("synthetic")

    with pytest.raises(exc):
        await always_fails()
    return calls


# multiplier != 2 and min_wait != multiplier distinguish the exponent base
# from the initial amplitude; max_wait=10 exercises the cap.
_CURVE_KW = {"max_attempts": 5, "min_wait": 1, "max_wait": 10, "multiplier": 3}
_CURVE = [1, 3, 9, 10]


@pytest.mark.parametrize("jitter", [True, False])
def test_retry_api_call_jitter_toggle_keeps_base_curve_sync(
    recorded_sleeps, jitter_noise, jitter
):
    calls = _exhaust_sync(
        retry_api_call(jitter=jitter, retry_on=_TemporaryError, **_CURVE_KW),
        _TemporaryError,
    )
    assert calls == 5
    assert recorded_sleeps == pytest.approx(_CURVE)


@pytest.mark.parametrize("jitter", [True, False])
async def test_retry_api_call_jitter_toggle_keeps_base_curve_async(
    recorded_sleeps, jitter_noise, jitter
):
    calls = await _exhaust_async(
        retry_api_call(jitter=jitter, retry_on=_TemporaryError, **_CURVE_KW),
        _TemporaryError,
    )
    assert calls == 5
    assert recorded_sleeps == pytest.approx(_CURVE)


def test_retry_api_call_jitter_adds_noise_and_caps_final_wait(
    recorded_sleeps, jitter_noise
):
    jitter_noise["noise"] = 0.5
    _exhaust_sync(
        retry_api_call(jitter=True, retry_on=_TemporaryError, **_CURVE_KW),
        _TemporaryError,
    )
    # Noise is added on top of the base curve, then the result is capped.
    assert recorded_sleeps == pytest.approx([1.5, 3.5, 9.5, 10])


def test_retry_api_call_no_jitter_defaults(recorded_sleeps):
    _exhaust_sync(
        retry_api_call(jitter=False, retry_on=_TemporaryError), _TemporaryError
    )
    assert recorded_sleeps == pytest.approx([1.0, 2.0])


def test_retry_file_op_default_curve_sync(recorded_sleeps):
    calls = _exhaust_sync(retry_file_op(), OSError)
    assert calls == 3
    assert recorded_sleeps == pytest.approx([0.1, 0.2])


async def test_retry_file_op_default_curve_async(recorded_sleeps):
    calls = await _exhaust_async(retry_file_op(), OSError)
    assert calls == 3
    assert recorded_sleeps == pytest.approx([0.1, 0.2])


@pytest.mark.parametrize("is_async", [False, True])
async def test_retry_file_op_matches_api_curve(recorded_sleeps, is_async):
    decorator = retry_file_op(retry_on=_TemporaryError, **_CURVE_KW)
    if is_async:
        calls = await _exhaust_async(decorator, _TemporaryError)
    else:
        calls = _exhaust_sync(decorator, _TemporaryError)
    assert calls == 5
    assert recorded_sleeps == pytest.approx(_CURVE)


@pytest.mark.parametrize("preset", ["api_jitter", "api_plain", "file"])
def test_single_attempt_never_sleeps(recorded_sleeps, preset):
    decorator = {
        "api_jitter": retry_api_call(max_attempts=1, jitter=True),
        "api_plain": retry_api_call(max_attempts=1, jitter=False),
        "file": retry_file_op(max_attempts=1, retry_on=Exception),
    }[preset]
    assert _exhaust_sync(decorator, _TemporaryError) == 1
    assert recorded_sleeps == []
