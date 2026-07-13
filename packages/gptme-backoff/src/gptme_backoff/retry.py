"""Retry decorators and utilities built on tenacity.

Provides sync/async retry decorators and convenience presets for API calls
and file operations with exponential backoff, jitter, and timeout support.
"""

from __future__ import annotations

__all__ = [
    "retry_api_call",
    "retry_async",
    "retry_file_op",
    "retry_sync",
]

import inspect
from typing import Any, Callable, TypeVar

import tenacity
from tenacity.retry import retry_base
from tenacity.stop import stop_base
from tenacity.wait import wait_base

T = TypeVar("T")


def _is_coroutine_callable(fn: Any) -> bool:
    """Return True if *fn* is async — either an async def or a callable object with async __call__."""
    return inspect.iscoroutinefunction(fn) or inspect.iscoroutinefunction(
        getattr(fn, "__call__", None)
    )


F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _default_before_sleep(
    log_path: str = "/dev/null",
) -> Callable[[tenacity.RetryCallState], None]:
    """Build a before_sleep callback that writes to *log_path*."""
    from pathlib import Path

    def _log(retry_state: tenacity.RetryCallState) -> None:
        attempt = retry_state.attempt_number
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        msg = f"[backoff] attempt {attempt} failed: {exc}"

        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            with Path(log_path).open("a") as f:
                f.write(msg + "\n")
        except OSError:
            pass

        import logging

        logging.getLogger("gptme_backoff").warning(msg)

    return _log


def _retry_error_callback_none(
    _retry_state: tenacity.RetryCallState,
) -> None:
    """Callback that returns None on exhaustion (for reraise=False)."""
    return None


# ---------------------------------------------------------------------------
# sync decorator
# ---------------------------------------------------------------------------


def retry_sync(
    stop: stop_base = tenacity.stop_after_attempt(3),
    wait: wait_base = tenacity.wait_exponential(min=0.5, max=30, multiplier=1.5),
    retry: retry_base = tenacity.retry_if_exception_type(Exception),
    reraise: bool = True,
    before_sleep: Callable[[tenacity.RetryCallState], Any] | None = None,
    **tenacity_kw: Any,
) -> Callable[[F], F]:
    """Decorator: retry a sync function.

    Parameters
    ----------
    stop : tenacity.stop_base
        Stop condition (default: 3 attempts).
    wait : tenacity.wait_base
        Wait strategy (default: exp backoff 0.5–30s, ×1.5).
    retry : tenacity.retry_base
        Retry condition (default: any Exception).
    reraise : bool
        If True (default), re-raise the last caught exception on exhaustion.
        If False, a ``tenacity.RetryError`` is raised wrapping the last exception.
    before_sleep : callable | None
        Called before each sleep with the RetryCallState.

    Usage::

        from gptme_backoff import retry_sync
        import tenacity

        @retry_sync(stop=tenacity.stop_after_attempt(5),
                    wait=tenacity.wait_fixed(1))
        def fetch(url: str) -> str:
            ...
    """
    dec = tenacity.Retrying(
        stop=stop,
        wait=wait,
        retry=retry,
        reraise=reraise,
        before_sleep=before_sleep or _default_before_sleep("/dev/null"),
        **tenacity_kw,
    )

    def wrapper(fn: F) -> F:
        return dec.wraps(fn)

    return wrapper


# ---------------------------------------------------------------------------
# async decorator
# ---------------------------------------------------------------------------


def retry_async(
    stop: stop_base = tenacity.stop_after_attempt(3),
    wait: wait_base = tenacity.wait_exponential(min=0.5, max=30, multiplier=1.5),
    retry: retry_base = tenacity.retry_if_exception_type(Exception),
    reraise: bool = True,
    before_sleep: Callable[[tenacity.RetryCallState], Any] | None = None,
    **tenacity_kw: Any,
) -> Callable[[F], F]:
    """Decorator: retry an async function.

    Uses ``tenacity.AsyncRetrying`` internally so the retry logic itself is
    async-compatible (does not block the event loop during waits).

    Parameters are identical to *retry_sync*.

    Usage::

        @retry_async(stop=tenacity.stop_after_attempt(5),
                     wait=tenacity.wait_fixed(1))
        async def fetch(url: str) -> str:
            ...
    """
    dec = tenacity.AsyncRetrying(
        stop=stop,
        wait=wait,
        retry=retry,
        reraise=reraise,
        before_sleep=before_sleep or _default_before_sleep("/dev/null"),
        **tenacity_kw,
    )

    def wrapper(fn: F) -> F:
        return dec.wraps(fn)

    return wrapper


# ---------------------------------------------------------------------------
# convenience presets (keyword-argument interface for callers who don't want
# to import tenacity themselves)
# ---------------------------------------------------------------------------


def _make_wait_strategy(
    min_wait: float,
    max_wait: float,
    multiplier: float,
    jitter: bool,
) -> wait_base:
    if jitter:
        return tenacity.wait_exponential_jitter(
            initial=min_wait,
            max=max_wait,
            exp_base=multiplier,
            jitter=min_wait,
        )
    return tenacity.wait_exponential(min=min_wait, max=max_wait, multiplier=multiplier)


def retry_api_call(
    max_attempts: int = 3,
    timeout: float | None = None,
    min_wait: float = 1.0,
    max_wait: float = 60.0,
    multiplier: float = 2.0,
    jitter: bool = True,
    retry_on: type[BaseException] | tuple[type[BaseException], ...] = Exception,
) -> Callable[[F], F]:
    """Convenience decorator tuned for API call retries.

    Combines exponential backoff with optional jitter and a hard wall-clock
    timeout via ``tenacity.stop_after_delay``.

    Parameters
    ----------
    max_attempts : int
        Maximum retry attempts (default 3).
    timeout : float | None
        Wall-clock timeout in seconds. ``None`` = no timeout.
    min_wait : float
        Minimum wait between retries, seconds (default 1.0).
    max_wait : float
        Maximum wait between retries, seconds (default 60.0).
    multiplier : float
        Exponential backoff multiplier (default 2.0).
    jitter : bool
        Add random jitter to wait times (default True).
    retry_on : exception type or tuple
        Which exceptions trigger a retry (default ``Exception``).

    Usage::

        from gptme_backoff import retry_api_call

        @retry_api_call(max_attempts=5, timeout=30)
        def call_api() -> dict:
            ...

    Applies to both sync and async functions (tenacity auto-detects).
    """
    stop_conditions: list[stop_base] = [tenacity.stop_after_attempt(max_attempts)]
    if timeout is not None:
        stop_conditions.append(tenacity.stop_after_delay(timeout))

    wait_strategy = _make_wait_strategy(min_wait, max_wait, multiplier, jitter)

    def decorator(fn: F) -> F:
        retry_decorator = retry_async if _is_coroutine_callable(fn) else retry_sync
        return retry_decorator(
            stop=tenacity.stop_any(*stop_conditions),
            wait=wait_strategy,
            retry=tenacity.retry_if_exception_type(retry_on),
        )(fn)

    return decorator


def retry_file_op(
    max_attempts: int = 3,
    min_wait: float = 0.1,
    max_wait: float = 5.0,
    multiplier: float = 2.0,
    retry_on: type[BaseException] | tuple[type[BaseException], ...] = OSError,
) -> Callable[[F], F]:
    """Convenience decorator tuned for file I/O retries.

    Only retries on *OSError* by default — transient IO errors (EAGAIN, EBUSY,
    ENOSPC). Non-OS exceptions (ValueError, TypeError) are not retried.

    Parameters
    ----------
    max_attempts : int
        Maximum retry attempts (default 3).
    min_wait : float
        Minimum wait, seconds (default 0.1).
    max_wait : float
        Maximum wait, seconds (default 5.0).
    multiplier : float
        Exponential backoff multiplier (default 2.0).
    retry_on : exception type or tuple
        Which exceptions trigger a retry (default ``OSError``).

    Usage::

        from gptme_backoff import retry_file_op

        @retry_file_op(max_attempts=5)
        def read_config() -> str:
            ...
    """

    def decorator(fn: F) -> F:
        retry_decorator = retry_async if _is_coroutine_callable(fn) else retry_sync
        return retry_decorator(
            stop=tenacity.stop_after_attempt(max_attempts),
            wait=tenacity.wait_exponential(
                min=min_wait, max=max_wait, multiplier=multiplier
            ),
            retry=tenacity.retry_if_exception_type(retry_on),
        )(fn)

    return decorator
