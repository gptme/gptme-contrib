"""Tests for the slot circuit breaker (cross-process credential-survival layer)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from gptme_block_registry.slot_circuit_breaker import (
    AUTH_DEATH,
    NEUTRAL,
    PRODUCTIVE,
    CircuitState,
    _apply_verdict,
    _load_breaker,
    _save_breaker,
    _SlotBreaker,
    decide_respawn,
)

# ---------------------------------------------------------------------------
# Unit tests for the in-process circuit-breaker state machine
# ---------------------------------------------------------------------------


class TestSlotBreaker:
    def _breaker(self, threshold: int = 3, cooldown: float = 300.0) -> _SlotBreaker:
        return _SlotBreaker(failure_threshold=threshold, cooldown=cooldown)

    def test_starts_closed(self) -> None:
        b = self._breaker()
        assert b.state == CircuitState.CLOSED
        assert b.failure_count == 0
        assert b.is_open(time.time()) is False

    def test_trips_on_threshold(self) -> None:
        b = self._breaker(threshold=3)
        now = time.time()
        b.record_failure(now)
        b.record_failure(now)
        assert b.state == CircuitState.CLOSED  # 2 failures, threshold=3
        b.record_failure(now)
        assert b.state == CircuitState.OPEN

    def test_open_suppresses(self) -> None:
        b = self._breaker(threshold=1)
        now = time.time()
        b.record_failure(now)
        assert b.is_open(now) is True

    def test_cooldown_transitions_to_half_open(self) -> None:
        b = self._breaker(threshold=1, cooldown=10.0)
        now = time.time()
        b.record_failure(now)
        assert b.is_open(now) is True
        # After cooldown, next is_open call transitions to HALF_OPEN and allows probe
        assert b.is_open(now + 11.0) is False
        assert b.state == CircuitState.HALF_OPEN

    def test_half_open_allows_exactly_one_probe(self) -> None:
        """Only one probe must pass through HALF_OPEN; all subsequent calls suppress."""
        b = self._breaker(threshold=1, cooldown=10.0)
        now = time.time()
        b.record_failure(now)
        # Transition to HALF_OPEN on first call after cooldown
        assert b.is_open(now + 11.0) is False  # first call: probe allowed
        # Subsequent calls must be suppressed until a verdict arrives
        assert b.is_open(now + 11.1) is True
        assert b.is_open(now + 11.2) is True

    def test_half_open_probe_failure_reopens(self) -> None:
        b = self._breaker(threshold=1, cooldown=10.0)
        now = time.time()
        b.record_failure(now)
        b.is_open(now + 11.0)  # → HALF_OPEN
        b.record_failure(now + 12.0)
        assert b.state == CircuitState.OPEN

    def test_successful_run_closes_breaker(self) -> None:
        b = self._breaker(threshold=2)
        now = time.time()
        b.record_failure(now)
        b.record_failure(now)
        assert b.state == CircuitState.OPEN
        # After cooldown: HALF_OPEN, then success closes it
        b.is_open(now + 400.0)
        assert b.state == CircuitState.HALF_OPEN
        b.record_success()
        assert b.state == CircuitState.CLOSED
        assert b.failure_count == 0

    def test_success_resets_closed_breaker(self) -> None:
        b = self._breaker(threshold=3)
        now = time.time()
        b.record_failure(now)
        b.record_success()
        assert b.failure_count == 0
        assert b.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------


class TestPersistence:
    def _breaker(self, **kw: object) -> _SlotBreaker:
        return _SlotBreaker(failure_threshold=3, cooldown=300.0, **kw)

    def test_closed_roundtrip(self) -> None:
        b = self._breaker()
        d = _save_breaker(b)
        b2 = _load_breaker(d, threshold=3, cooldown=300.0)
        assert b2.state == CircuitState.CLOSED
        assert b2.failure_count == 0
        assert b2._opened_at is None

    def test_open_roundtrip_preserves_opened_at(self) -> None:
        b = self._breaker()
        now = 1700000000.0
        b.record_failure(now)
        b.record_failure(now)
        b.record_failure(now)  # trips
        assert b.state == CircuitState.OPEN
        d = _save_breaker(b)
        b2 = _load_breaker(d, threshold=3, cooldown=300.0)
        assert b2.state == CircuitState.OPEN
        assert b2._opened_at == pytest.approx(now)

    def test_garbled_state_defaults_to_closed(self) -> None:
        b = _load_breaker({"state": "INVALID_STATE", "failure_count": 2}, 3, 300.0)
        assert b.state == CircuitState.CLOSED

    def test_missing_state_defaults_to_closed(self) -> None:
        b = _load_breaker({}, 3, 300.0)
        assert b.state == CircuitState.CLOSED
        assert b.failure_count == 0

    def test_malformed_failure_count_resets_to_closed(self) -> None:
        """A non-int failure_count (e.g. 'invalid') must reset the slot, not raise."""
        b = _load_breaker({"failure_count": "invalid", "state": "OPEN"}, 3, 300.0)
        assert b.state == CircuitState.CLOSED
        assert b.failure_count == 0

    def test_malformed_slot_value_resets_to_closed(self) -> None:
        """A slot value that isn't a dict (e.g. bare int) must reset, not raise."""
        # _load_breaker receives only the slot-level dict; upstream parse already
        # resets a non-dict top-level. But the outer try/except in _load_breaker
        # also covers any AttributeError / TypeError from a bad slot payload.
        b = _load_breaker({"opened_at": "not-a-float", "state": "OPEN"}, 3, 300.0)
        # opened_at parse error is silently reset to None; state is preserved
        assert b.state == CircuitState.OPEN
        assert b._opened_at is None


# ---------------------------------------------------------------------------
# _apply_verdict unit tests
# ---------------------------------------------------------------------------


class TestApplyVerdict:
    def _closed(self, threshold: int = 3) -> _SlotBreaker:
        return _SlotBreaker(failure_threshold=threshold, cooldown=300.0)

    def _open(self) -> _SlotBreaker:
        b = _SlotBreaker(failure_threshold=1, cooldown=300.0)
        b.record_failure(time.time())
        return b

    def test_neutral_closed_no_change(self) -> None:
        b = self._closed()
        now = time.time()
        suppress = _apply_verdict(b, NEUTRAL, now)
        assert suppress is False
        assert b.state == CircuitState.CLOSED
        assert b.failure_count == 0

    def test_neutral_open_suppresses_without_recording(self) -> None:
        b = self._open()
        now = time.time()
        before_count = b.failure_count
        suppress = _apply_verdict(b, NEUTRAL, now)
        assert suppress is True
        assert b.failure_count == before_count  # unchanged

    def test_auth_death_increments_failure(self) -> None:
        b = self._closed(threshold=3)
        suppress = _apply_verdict(b, AUTH_DEATH, time.time())
        assert suppress is False
        assert b.failure_count == 1

    def test_auth_death_trips_on_threshold(self) -> None:
        b = _SlotBreaker(failure_threshold=2, cooldown=300.0)
        now = time.time()
        _apply_verdict(b, AUTH_DEATH, now)
        suppress = _apply_verdict(b, AUTH_DEATH, now)
        assert suppress is True
        assert b.state == CircuitState.OPEN

    def test_auth_death_while_open_suppresses(self) -> None:
        b = self._open()
        suppress = _apply_verdict(b, AUTH_DEATH, time.time())
        assert suppress is True

    def test_productive_on_closed_resets(self) -> None:
        b = self._closed()
        now = time.time()
        _apply_verdict(b, AUTH_DEATH, now)
        suppress = _apply_verdict(b, PRODUCTIVE, now)
        assert suppress is False
        assert b.failure_count == 0

    def test_productive_while_open_suppresses(self) -> None:
        # Breaker open, but the "productive" run happened before this decision
        b = self._open()
        suppress = _apply_verdict(b, PRODUCTIVE, time.time())
        assert suppress is True
        assert b.state == CircuitState.OPEN  # not closed prematurely


# ---------------------------------------------------------------------------
# Integration: decide_respawn with a real state file
# ---------------------------------------------------------------------------


class TestDecideRespawn:
    def test_new_item_proceeds(self, tmp_path: Path) -> None:
        sf = tmp_path / "cb.json"
        suppress, state = decide_respawn(state_file=sf, slot="alice", verdict=NEUTRAL)
        assert suppress is False
        assert state == "CLOSED"

    def test_consecutive_auth_deaths_trip_breaker(self, tmp_path: Path) -> None:
        sf = tmp_path / "cb.json"
        for _ in range(3):
            decide_respawn(state_file=sf, slot="bob", verdict=AUTH_DEATH, threshold=3)
        suppress, state = decide_respawn(
            state_file=sf, slot="bob", verdict=NEUTRAL, threshold=3
        )
        assert suppress is True
        assert state == "OPEN"

    def test_productive_closes_after_cooldown(self, tmp_path: Path) -> None:
        sf = tmp_path / "cb.json"
        # Trip the breaker
        for _ in range(3):
            decide_respawn(state_file=sf, slot="bob", verdict=AUTH_DEATH, threshold=3)
        # Without cooldown elapsed, PRODUCTIVE while OPEN still suppresses
        suppress, state = decide_respawn(
            state_file=sf, slot="bob", verdict=PRODUCTIVE, threshold=3
        )
        assert suppress is True

    def test_cooldown_allows_half_open_probe(self, tmp_path: Path) -> None:
        sf = tmp_path / "cb.json"
        # Trip the breaker
        for _ in range(2):
            decide_respawn(state_file=sf, slot="bob", verdict=AUTH_DEATH, threshold=2)
        # Manually backdate opened_at to simulate elapsed cooldown
        data = json.loads(sf.read_text())
        data["bob"]["opened_at"] = time.time() - 999.0
        sf.write_text(json.dumps(data))
        suppress, state = decide_respawn(
            state_file=sf, slot="bob", verdict=NEUTRAL, threshold=2
        )
        assert suppress is False
        assert state == "HALF_OPEN"

    def test_slots_are_independent(self, tmp_path: Path) -> None:
        sf = tmp_path / "cb.json"
        for _ in range(3):
            decide_respawn(state_file=sf, slot="alice", verdict=AUTH_DEATH, threshold=3)
        # bob's slot should be unaffected
        suppress, _ = decide_respawn(
            state_file=sf, slot="bob", verdict=NEUTRAL, threshold=3
        )
        assert suppress is False

    def test_neutral_does_not_reset_death_run(self, tmp_path: Path) -> None:
        sf = tmp_path / "cb.json"
        decide_respawn(state_file=sf, slot="bob", verdict=AUTH_DEATH, threshold=3)
        decide_respawn(state_file=sf, slot="bob", verdict=AUTH_DEATH, threshold=3)
        # NEUTRAL (new work item) must not reset the consecutive-death count
        decide_respawn(state_file=sf, slot="bob", verdict=NEUTRAL, threshold=3)
        # Third death should still trip
        suppress, state = decide_respawn(
            state_file=sf, slot="bob", verdict=AUTH_DEATH, threshold=3
        )
        assert suppress is True
        assert state == "OPEN"

    def test_invalid_verdict_raises(self, tmp_path: Path) -> None:
        sf = tmp_path / "cb.json"
        with pytest.raises(ValueError, match="unknown verdict"):
            decide_respawn(state_file=sf, slot="bob", verdict="invalid")
