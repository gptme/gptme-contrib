"""Wall-clock corrections must not bypass breaker timing windows."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from gptme_block_registry.slot_circuit_breaker import (
    AUTH_DEATH,
    DEFAULT_COOLDOWN,
    DEFAULT_PROBE_TIMEOUT,
    PRODUCTIVE,
    decide_respawn,
)


@pytest.mark.parametrize("rollback", [2.0, 86_400.0])
def test_open_rollback_restarts_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rollback: float
) -> None:
    now = [100_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    sf = tmp_path / "breaker.json"
    decide_respawn(state_file=sf, slot="bob", verdict=AUTH_DEATH, threshold=1)
    now[0] -= rollback
    corrected = now[0]
    assert decide_respawn(state_file=sf, slot="bob") == (True, "OPEN")
    assert json.loads(sf.read_text())["bob"]["opened_at"] == corrected
    now[0] += DEFAULT_COOLDOWN - 1
    assert decide_respawn(state_file=sf, slot="bob") == (True, "OPEN")
    now[0] += 1
    assert decide_respawn(state_file=sf, slot="bob") == (False, "HALF_OPEN")


@pytest.mark.parametrize("verdict", [None, PRODUCTIVE, AUTH_DEATH])
def test_probe_rollback_preserves_one_probe_and_its_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str | None
) -> None:
    now = [100_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    sf = tmp_path / "breaker.json"
    decide_respawn(state_file=sf, slot="bob", verdict=AUTH_DEATH, threshold=1)
    now[0] += DEFAULT_COOLDOWN
    grant = now[0]
    assert decide_respawn(state_file=sf, slot="bob") == (False, "HALF_OPEN")
    now[0] -= 2 if verdict is not None else 86_400
    corrected = now[0]
    assert decide_respawn(state_file=sf, slot="bob") == (True, "HALF_OPEN")
    # Persist the corrected window so each reload does not restart it again.
    saved = json.loads(sf.read_text())["bob"]
    assert saved["probe_started_at"] == grant
    assert saved["probe_timeout_started_at"] == corrected
    now[0] += 1
    assert decide_respawn(state_file=sf, slot="bob") == (True, "HALF_OPEN")
    if verdict is not None:
        # The public API rejects future attempt timestamps. Once the clock
        # catches up, the original probe can still settle the breaker.
        now[0] = grant + 1
        assert decide_respawn(
            state_file=sf, slot="bob", verdict=verdict, verdict_started_at=grant
        ) == ((False, "CLOSED") if verdict == PRODUCTIVE else (True, "OPEN"))
    else:
        now[0] = corrected + DEFAULT_PROBE_TIMEOUT - 1
        assert decide_respawn(state_file=sf, slot="bob") == (True, "HALF_OPEN")
        now[0] += 1
        assert decide_respawn(state_file=sf, slot="bob") == (False, "HALF_OPEN")


def test_rollback_does_not_admit_a_pre_probe_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [100_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    sf = tmp_path / "breaker.json"
    decide_respawn(state_file=sf, slot="bob", verdict=AUTH_DEATH, threshold=1)
    now[0] = 100_300.0
    assert decide_respawn(state_file=sf, slot="bob") == (False, "HALF_OPEN")
    now[0] = 99_900.0
    assert decide_respawn(state_file=sf, slot="bob") == (True, "HALF_OPEN")
    now[0] = 99_960.0
    assert decide_respawn(
        state_file=sf, slot="bob", verdict=PRODUCTIVE, verdict_started_at=99_950.0
    ) == (True, "HALF_OPEN")


def test_recovery_watermark_does_not_disable_failures_after_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [100_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    sf = tmp_path / "breaker.json"
    decide_respawn(state_file=sf, slot="bob", verdict=AUTH_DEATH, threshold=1)
    now[0] = 100_300.0
    assert decide_respawn(state_file=sf, slot="bob") == (False, "HALF_OPEN")
    now[0] += 1
    assert decide_respawn(
        state_file=sf, slot="bob", verdict=PRODUCTIVE, verdict_started_at=100_300.0
    ) == (False, "CLOSED")
    now[0] = 13_900.0
    for started in (13_901.0, 13_902.0, 13_903.0):
        now[0] = started + 1
        result = decide_respawn(
            state_file=sf, slot="bob", verdict=AUTH_DEATH, verdict_started_at=started
        )
    assert result == (True, "OPEN")
    assert json.loads(sf.read_text())["bob"]["failure_count"] == 3
