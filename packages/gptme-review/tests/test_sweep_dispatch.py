"""Host-independent sweep scheduling contracts, without GitHub or credentials."""

from __future__ import annotations

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest
from gptme_review import sweep


def pr(number: int) -> dict:
    return {"repo": "example/project", "number": number}


class Host:
    def __init__(self) -> None:
        self.ran: list[int] = []
        self.admissions: list[tuple[int, bool]] = []
        self.reason: str | None = None

    def admission(self, candidate: dict, elapsed: float, started: bool):
        assert elapsed >= 0
        self.admissions.append((candidate["number"], started))
        return sweep.DispatchAdmission()

    def run_review(self, candidate: dict) -> None:
        self.ran.append(candidate["number"])

    def stop_reason(self) -> str | None:
        return self.reason


def dispatch(candidates: list[dict], host: Host, *, workers: int = 1):
    # Assert the public API explicitly: missing extraction is a behavioral RED,
    # not an import error hiding unrelated test failures.
    assert hasattr(sweep, "dispatch_reviews"), "sweep has no portable dispatcher"
    return sweep.dispatch_reviews(candidates, host, workers=workers)


def test_serial_dispatch_and_accounting():
    host = Host()
    result = dispatch([pr(1), pr(2)], host)
    assert host.ran == [1, 2]
    assert host.admissions == [(1, False), (2, True)]
    assert result.attempted == 2
    assert result.budget_skipped == []
    assert result.size_deferred == []
    assert result.stop_reason is None


def test_size_deferral_does_not_starve_smaller_review():
    class SizedHost(Host):
        def admission(self, candidate, elapsed, started):
            if candidate["number"] == 1:
                return sweep.DispatchAdmission("three waves do not fit", defer=True)
            return super().admission(candidate, elapsed, started)

    host = SizedHost()
    result = dispatch([pr(1), pr(2)], host)
    assert host.ran == [2]
    assert host.admissions == [(2, False)]
    assert result.attempted == 1
    assert result.size_deferred == [pr(1)]
    assert not result.budget_skipped


def test_deadline_stop_defers_entire_remaining_tail():
    class DeadlineHost(Host):
        def admission(self, candidate, elapsed, started):
            if started:
                return sweep.DispatchAdmission("deadline")
            return super().admission(candidate, elapsed, started)

    host = DeadlineHost()
    result = dispatch([pr(1), pr(2), pr(3)], host)
    assert host.ran == [1]
    assert result.budget_skipped == [pr(2), pr(3)]
    assert result.stop_reason == "deadline"


def test_breaker_stops_new_submissions():
    class BreakerHost(Host):
        def run_review(self, candidate):
            super().run_review(candidate)
            self.reason = "quota exhausted"

    host = BreakerHost()
    result = dispatch([pr(1), pr(2), pr(3)], host)
    assert host.ran == [1]
    assert result.attempted == 1
    assert result.budget_skipped == [pr(2), pr(3)]
    assert result.stop_reason == "quota exhausted"


def test_existing_breaker_starts_nothing():
    host = Host()
    host.reason = "budget unavailable"
    result = dispatch([pr(1)], host)
    assert host.ran == []
    assert host.admissions == []
    assert result.attempted == 0
    assert result.budget_skipped == [pr(1)]


def test_empty_queue():
    result = dispatch([], Host())
    assert result.attempted == 0
    assert result.stop_reason is None


def test_unexpected_worker_exception_propagates():
    class BrokenHost(Host):
        def run_review(self, candidate):
            raise RuntimeError("publication hook failed")

    with pytest.raises(RuntimeError, match="publication hook failed"):
        dispatch([pr(1)], BrokenHost())


def test_concurrency_is_bounded_and_duplicate_inflight_is_skipped():
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active: Counter[int] = Counter()
    max_active = 0

    class ConcurrentHost(Host):
        def run_review(self, candidate):
            nonlocal max_active
            with lock:
                active[candidate["number"]] += 1
                assert active[candidate["number"]] == 1
                max_active = max(max_active, sum(active.values()))
                self.ran.append(candidate["number"])
                if sum(active.values()) == 2:
                    entered.set()
            assert release.wait(5), "test did not release workers"
            with lock:
                active[candidate["number"]] -= 1

    host = ConcurrentHost()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            dispatch, [pr(1), pr(1), pr(2), pr(3)], host, workers=2
        )
        try:
            assert entered.wait(5), "dispatcher did not fill its worker pool"
            assert sorted(host.ran) == [1, 2]
        finally:
            release.set()
        result = future.result(timeout=5)
    assert max_active == 2
    assert sorted(host.ran) == [1, 2, 3]
    assert result.attempted == 3


@pytest.mark.parametrize("workers", [0, -1])
def test_invalid_worker_count_is_rejected(workers):
    with pytest.raises(ValueError, match="workers"):
        dispatch([pr(1)], Host(), workers=workers)


def test_admission_uses_host_start_and_rechecks_elapsed_for_each_submission():
    elapsed_seen = []
    ticks = iter([110.0, 125.0])

    class ClockHost(Host):
        def admission(self, candidate, elapsed, started):
            elapsed_seen.append(elapsed)
            return super().admission(candidate, elapsed, started)

    result = sweep.dispatch_reviews(
        [pr(1), pr(2)], ClockHost(), started=100.0, clock=lambda: next(ticks)
    )
    assert result.attempted == 2
    assert elapsed_seen == [10.0, 25.0]


def test_breaker_drains_already_running_workers_before_returning():
    both_running = threading.Barrier(2)
    breaker_set = threading.Event()
    release = threading.Event()

    class DrainingHost(Host):
        def run_review(self, candidate):
            both_running.wait(timeout=5)
            if candidate["number"] == 1:
                self.reason = "quota exhausted"
                breaker_set.set()
            else:
                assert release.wait(5)
            self.ran.append(candidate["number"])

    host = DrainingHost()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(dispatch, [pr(1), pr(2), pr(3)], host, workers=2)
        try:
            assert breaker_set.wait(5)
            assert not future.done()
        finally:
            release.set()
        result = future.result(timeout=5)
    assert sorted(host.ran) == [1, 2]
    assert result.attempted == 2
    assert result.budget_skipped == [pr(3)]
    assert result.stop_reason == "quota exhausted"


def test_defer_requires_a_reason():
    with pytest.raises(ValueError, match="reason"):
        sweep.DispatchAdmission(defer=True)


def test_breaker_is_rechecked_after_slow_admission():
    class AdmissionBreakerHost(Host):
        def admission(self, candidate, elapsed, started):
            self.reason = "quota exhausted during admission"
            return sweep.DispatchAdmission()

    host = AdmissionBreakerHost()
    result = dispatch([pr(1), pr(2)], host)
    assert host.ran == []
    assert result.attempted == 0
    assert result.budget_skipped == [pr(1), pr(2)]
    assert result.stop_reason == "quota exhausted during admission"


def test_final_batch_breaker_is_reported_even_without_skipped_work():
    class FinalBreakerHost(Host):
        def run_review(self, candidate):
            super().run_review(candidate)
            self.reason = "quota exhausted"

    result = dispatch([pr(1)], FinalBreakerHost())
    assert result.attempted == 1
    assert result.budget_skipped == []
    assert result.stop_reason == "quota exhausted"
