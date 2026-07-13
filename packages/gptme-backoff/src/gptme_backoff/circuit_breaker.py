"""Circuit breaker for flaky external calls.

Provides CLOSED → OPEN (on failure threshold) → HALF_OPEN (after cooldown)
→ CLOSED (on probe success) state machine to prevent cascading failures from
flaky MCP servers or external APIs.

Usage::

    from gptme_backoff import CircuitBreaker, CircuitBreakerOpen

    cb = CircuitBreaker(name="mcp-filesystem", failure_threshold=5, cooldown=30.0)

    # Direct call interface
    try:
        result = cb.call(my_mcp_function, *args, **kwargs)
    except CircuitBreakerOpen as e:
        # Fast-fail: don't even attempt the call
        ...

    # Decorator interface
    @cb.wrap
    def call_mcp_tool(*args, **kwargs):
        ...
"""

from __future__ import annotations

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerOpen",
    "State",
]

import inspect
import logging
import threading
import time
from enum import Enum
from typing import Any, Callable, TypeVar

log = logging.getLogger("gptme_backoff.circuit_breaker")

T = TypeVar("T")
F = TypeVar("F", bound=Callable[..., Any])


class State(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpen(Exception):
    """Raised when a call is rejected because the circuit breaker is OPEN."""

    def __init__(self, name: str, retry_after: float | None = None) -> None:
        self.name = name
        self.retry_after = retry_after
        msg = f"Circuit breaker '{name}' is OPEN"
        if retry_after is not None:
            msg += f" (retry after {retry_after:.1f}s)"
        super().__init__(msg)


class CircuitBreaker:
    """Three-state circuit breaker: CLOSED → OPEN → HALF_OPEN → CLOSED.

    CLOSED: calls pass through; consecutive failures increment counter.
    OPEN: calls rejected immediately after failure_threshold consecutive failures.
    HALF_OPEN: after cooldown, a single probe call is allowed.
        - probe success → CLOSED (counter reset)
        - probe failure → OPEN (cooldown timer reset)

    Thread-safe: all state transitions use an internal lock.

    Parameters
    ----------
    name : str
        Human-readable name for logging and error messages.
    failure_threshold : int
        Consecutive failures needed to open the breaker (default 5).
    cooldown : float
        Seconds to wait in OPEN state before allowing a probe (default 30.0).
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        cooldown: float = 30.0,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown

        self._lock = threading.Lock()
        self._state = State.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def state(self) -> State:
        """Current state (may evaluate OPEN→HALF_OPEN transition lazily)."""
        with self._lock:
            return self._evaluate_state()

    def _evaluate_state(self) -> State:
        """Evaluate state, transitioning OPEN→HALF_OPEN when cooldown elapsed.

        Must be called with _lock held.
        """
        if self._state == State.OPEN and self._opened_at is not None:
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self.cooldown:
                self._state = State.HALF_OPEN
                self._probe_in_flight = False
                log.info(
                    "Circuit breaker '%s' → HALF_OPEN (cooldown elapsed)", self.name
                )
        return self._state

    def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Call *fn* through the circuit breaker.

        Raises CircuitBreakerOpen if the breaker is OPEN or a probe is
        already in-flight in HALF_OPEN state.
        """
        with self._lock:
            state = self._evaluate_state()

            if state == State.OPEN:
                retry_after = None
                if self._opened_at is not None:
                    retry_after = max(
                        0.0, self.cooldown - (time.monotonic() - self._opened_at)
                    )
                raise CircuitBreakerOpen(self.name, retry_after=retry_after)

            if state == State.HALF_OPEN:
                if self._probe_in_flight:
                    raise CircuitBreakerOpen(self.name)
                self._probe_in_flight = True

        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            self._on_failure(exc)
            raise
        except BaseException:
            # CancelledError, KeyboardInterrupt, etc. — clear probe so the
            # breaker doesn't stay permanently stuck in HALF_OPEN.
            with self._lock:
                if self._state == State.HALF_OPEN:
                    self._probe_in_flight = False
            raise
        else:
            self._on_success()
            return result

    async def async_call(
        self, fn: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Async version of *call* for coroutine functions.

        Awaits the coroutine so that exceptions raised inside it are captured
        by the circuit-breaker logic, not by the caller's event loop.
        """
        with self._lock:
            state = self._evaluate_state()

            if state == State.OPEN:
                retry_after = None
                if self._opened_at is not None:
                    retry_after = max(
                        0.0, self.cooldown - (time.monotonic() - self._opened_at)
                    )
                raise CircuitBreakerOpen(self.name, retry_after=retry_after)

            if state == State.HALF_OPEN:
                if self._probe_in_flight:
                    raise CircuitBreakerOpen(self.name)
                self._probe_in_flight = True

        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            self._on_failure(exc)
            raise
        except BaseException:
            with self._lock:
                if self._state == State.HALF_OPEN:
                    self._probe_in_flight = False
            raise
        else:
            self._on_success()
            return result

    def _on_success(self) -> None:
        with self._lock:
            if self._state in (State.HALF_OPEN, State.CLOSED):
                if self._state == State.HALF_OPEN:
                    log.info(
                        "Circuit breaker '%s' → CLOSED (probe succeeded)", self.name
                    )
                self._state = State.CLOSED
                self._failure_count = 0
                self._opened_at = None
                self._probe_in_flight = False

    def _on_failure(self, exc: Exception) -> None:
        with self._lock:
            if self._state == State.HALF_OPEN:
                self._state = State.OPEN
                self._opened_at = time.monotonic()
                self._probe_in_flight = False
                log.warning(
                    "Circuit breaker '%s' probe failed (%s) → OPEN (cooldown reset)",
                    self.name,
                    exc,
                )
            elif self._state == State.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self.failure_threshold:
                    self._state = State.OPEN
                    self._opened_at = time.monotonic()
                    log.warning(
                        "Circuit breaker '%s' → OPEN after %d consecutive failures",
                        self.name,
                        self._failure_count,
                    )

    def reset(self) -> None:
        """Manually reset to CLOSED state (e.g. after service recovery)."""
        with self._lock:
            self._state = State.CLOSED
            self._failure_count = 0
            self._opened_at = None
            self._probe_in_flight = False
        log.info("Circuit breaker '%s' manually reset to CLOSED", self.name)

    def wrap(self, fn: F) -> F:
        """Decorator: wrap a callable with this circuit breaker.

        Async functions are wrapped with *async_call* so that exceptions raised
        inside the coroutine body are captured, not just coroutine creation.
        """
        from functools import wraps

        if inspect.iscoroutinefunction(fn):

            @wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await self.async_call(fn, *args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return self.call(fn, *args, **kwargs)

        return wrapper  # type: ignore[return-value]

    def __repr__(self) -> str:
        return (
            f"CircuitBreaker(name={self.name!r}, state={self._state.value}, "
            f"failures={self._failure_count}/{self.failure_threshold})"
        )
