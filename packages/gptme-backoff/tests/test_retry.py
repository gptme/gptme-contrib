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
    """stop_after_delay limits the total retry budget, not per-call execution time."""
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
