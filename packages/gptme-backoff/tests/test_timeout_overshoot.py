"""retry_api_call(timeout=...) is documented as a hard wall-clock timeout but re-invokes past it."""

import time

import pytest
from gptme_backoff import retry_api_call


def test_retry_api_call_does_not_start_attempt_after_timeout(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    start = clock[0]
    call_times = []

    @retry_api_call(
        max_attempts=3, timeout=5.0, min_wait=10.0, max_wait=60.0, jitter=False
    )
    def call():
        call_times.append(clock[0] - start)
        raise RuntimeError("transient")

    with pytest.raises(RuntimeError):
        call()
    # "hard wall-clock timeout" of 5s: no attempt should start after t=5
    assert all(t <= 5.0 for t in call_times), call_times
