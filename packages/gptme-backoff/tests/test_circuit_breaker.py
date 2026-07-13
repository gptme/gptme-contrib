"""Tests for CircuitBreaker: all state transitions, concurrent access, edge cases."""

from __future__ import annotations

import threading
import time

import pytest
from gptme_backoff.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpen,
    State,
)


def _failing(exc: Exception = RuntimeError("boom")) -> None:
    raise exc


def _passing(value: int = 42) -> int:
    return value


# ── Basic state transitions ────────────────────────────────────────────────


def test_initial_state_is_closed():
    cb = CircuitBreaker("test", failure_threshold=3, cooldown=30.0)
    assert cb.state == State.CLOSED


def test_success_in_closed_state():
    cb = CircuitBreaker("test", failure_threshold=3, cooldown=30.0)
    result = cb.call(_passing, 7)
    assert result == 7
    assert cb.state == State.CLOSED


def test_failure_below_threshold_stays_closed():
    cb = CircuitBreaker("test", failure_threshold=3, cooldown=30.0)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            cb.call(_failing)
    assert cb.state == State.CLOSED
    assert cb._failure_count == 2


def test_failure_at_threshold_opens():
    cb = CircuitBreaker("test", failure_threshold=3, cooldown=30.0)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            cb.call(_failing)
    assert cb.state == State.OPEN


def test_call_rejected_when_open():
    cb = CircuitBreaker("test", failure_threshold=1, cooldown=60.0)
    with pytest.raises(RuntimeError):
        cb.call(_failing)
    assert cb.state == State.OPEN

    with pytest.raises(CircuitBreakerOpen) as exc_info:
        cb.call(_passing)
    assert exc_info.value.name == "test"


def test_retry_after_populated_when_open():
    cb = CircuitBreaker("test", failure_threshold=1, cooldown=10.0)
    with pytest.raises(RuntimeError):
        cb.call(_failing)
    try:
        cb.call(_passing)
    except CircuitBreakerOpen as e:
        assert e.retry_after is not None
        assert 0.0 <= e.retry_after <= 10.0


# ── HALF_OPEN via fake clock ───────────────────────────────────────────────


def test_transitions_to_half_open_after_cooldown(monkeypatch):
    cb = CircuitBreaker("test", failure_threshold=1, cooldown=5.0)
    start = time.monotonic()
    monkeypatch.setattr("time.monotonic", lambda: start)

    with pytest.raises(RuntimeError):
        cb.call(_failing)
    assert cb.state == State.OPEN

    # Advance clock past cooldown
    monkeypatch.setattr("time.monotonic", lambda: start + 6.0)
    assert cb.state == State.HALF_OPEN


def test_probe_success_closes_from_half_open(monkeypatch):
    cb = CircuitBreaker("test", failure_threshold=1, cooldown=5.0)
    start = time.monotonic()
    monkeypatch.setattr("time.monotonic", lambda: start)

    with pytest.raises(RuntimeError):
        cb.call(_failing)

    monkeypatch.setattr("time.monotonic", lambda: start + 6.0)
    assert cb.state == State.HALF_OPEN

    result = cb.call(_passing, 99)
    assert result == 99
    assert cb.state == State.CLOSED
    assert cb._failure_count == 0


def test_probe_failure_reopens_from_half_open(monkeypatch):
    cb = CircuitBreaker("test", failure_threshold=1, cooldown=5.0)
    start = time.monotonic()
    monkeypatch.setattr("time.monotonic", lambda: start)

    with pytest.raises(RuntimeError):
        cb.call(_failing)

    monkeypatch.setattr("time.monotonic", lambda: start + 6.0)
    assert cb.state == State.HALF_OPEN

    with pytest.raises(RuntimeError):
        cb.call(_failing)
    assert cb.state == State.OPEN


def test_probe_failure_resets_cooldown_timer(monkeypatch):
    cb = CircuitBreaker("test", failure_threshold=1, cooldown=5.0)
    tick = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: tick[0])

    with pytest.raises(RuntimeError):
        cb.call(_failing)

    tick[0] = 6.0  # cooldown elapsed → HALF_OPEN
    assert cb.state == State.HALF_OPEN

    with pytest.raises(RuntimeError):
        cb.call(_failing)  # probe fails → OPEN with timer reset to tick[0]=6.0

    tick[0] = 7.0  # only 1s after re-open; cooldown not elapsed yet
    assert cb.state == State.OPEN

    tick[0] = 12.0  # 6s after re-open; cooldown elapsed → HALF_OPEN again
    assert cb.state == State.HALF_OPEN


def test_only_one_probe_in_half_open(monkeypatch):
    """Second concurrent call in HALF_OPEN is rejected immediately."""
    cb = CircuitBreaker("test", failure_threshold=1, cooldown=5.0)
    start = time.monotonic()
    monkeypatch.setattr("time.monotonic", lambda: start)

    with pytest.raises(RuntimeError):
        cb.call(_failing)

    monkeypatch.setattr("time.monotonic", lambda: start + 6.0)
    assert cb.state == State.HALF_OPEN

    # Manually set probe_in_flight as if a first probe is running
    cb._probe_in_flight = True

    with pytest.raises(CircuitBreakerOpen):
        cb.call(_passing)


# ── Manual reset ──────────────────────────────────────────────────────────


def test_reset_restores_closed_state():
    cb = CircuitBreaker("test", failure_threshold=1, cooldown=60.0)
    with pytest.raises(RuntimeError):
        cb.call(_failing)
    assert cb.state == State.OPEN

    cb.reset()
    assert cb.state == State.CLOSED
    assert cb._failure_count == 0

    result = cb.call(_passing, 5)
    assert result == 5


# ── Decorator interface ────────────────────────────────────────────────────


def test_wrap_decorator_passes_through():
    cb = CircuitBreaker("test", failure_threshold=5, cooldown=30.0)

    @cb.wrap
    def add(a: int, b: int) -> int:
        return a + b

    assert add(2, 3) == 5


def test_wrap_decorator_opens_on_failures():
    cb = CircuitBreaker("test", failure_threshold=2, cooldown=30.0)

    @cb.wrap
    def boom() -> None:
        raise ValueError("fail")

    with pytest.raises(ValueError):
        boom()
    with pytest.raises(ValueError):
        boom()

    assert cb.state == State.OPEN
    with pytest.raises(CircuitBreakerOpen):
        boom()


# ── Success resets counter in CLOSED state ────────────────────────────────


def test_success_resets_failure_count():
    cb = CircuitBreaker("test", failure_threshold=3, cooldown=30.0)
    with pytest.raises(RuntimeError):
        cb.call(_failing)
    with pytest.raises(RuntimeError):
        cb.call(_failing)
    assert cb._failure_count == 2

    cb.call(_passing)
    assert cb._failure_count == 0
    assert cb.state == State.CLOSED


# ── Concurrent access ─────────────────────────────────────────────────────


def test_concurrent_failures_open_exactly_once():
    """Multiple threads failing simultaneously should open the breaker once."""
    cb = CircuitBreaker("test", failure_threshold=5, cooldown=30.0)
    errors: list[Exception] = []

    def worker() -> None:
        for _ in range(3):
            try:
                cb.call(_failing)
            except (RuntimeError, CircuitBreakerOpen) as e:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # At some point the breaker must have opened
    assert cb.state == State.OPEN
    # Some errors are CircuitBreakerOpen (fast-fail), some are RuntimeError
    open_errors = [e for e in errors if isinstance(e, CircuitBreakerOpen)]
    assert len(open_errors) >= 1


def test_concurrent_calls_in_closed_state_thread_safe():
    """Concurrent successes in CLOSED state must not corrupt the counter."""
    cb = CircuitBreaker("test", failure_threshold=10, cooldown=30.0)
    results: list[int] = []

    def worker(n: int) -> None:
        results.append(cb.call(_passing, n))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 20
    assert cb.state == State.CLOSED
    assert cb._failure_count == 0


# ── Edge cases ────────────────────────────────────────────────────────────


def test_failure_threshold_of_one():
    cb = CircuitBreaker("test", failure_threshold=1, cooldown=30.0)
    with pytest.raises(RuntimeError):
        cb.call(_failing)
    assert cb.state == State.OPEN


def test_repr():
    cb = CircuitBreaker("svc", failure_threshold=5, cooldown=30.0)
    r = repr(cb)
    assert "svc" in r
    assert "CLOSED" in r
    assert "0/5" in r


def test_circuit_breaker_open_str():
    exc = CircuitBreakerOpen("my-tool", retry_after=12.5)
    assert "my-tool" in str(exc)
    assert "12.5" in str(exc)

    exc2 = CircuitBreakerOpen("my-tool")
    assert "retry after" not in str(exc2)
