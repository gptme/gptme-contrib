"""Tests for gptme_runloops.pm_noop_streak."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from gptme_runloops.pm_noop_streak import (
    DEFAULT_BACKOFF_MINUTES,
    DEFAULT_BACKOFF_N,
    DEFAULT_STATE_FILENAME,
    NoopStreakDetector,
)


@pytest.fixture()
def state_file(tmp_path: Path) -> Path:
    return tmp_path / "pm-noop-streak.json"


@pytest.fixture()
def detector(state_file: Path) -> NoopStreakDetector:
    return NoopStreakDetector(state_path=state_file, backoff_n=2, backoff_minutes=30)


# --- Default path ---


def test_default_state_path_uses_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    d = NoopStreakDetector()
    assert d.state_path == tmp_path / DEFAULT_STATE_FILENAME


# --- should_skip returns a tuple ---


def test_should_skip_returns_tuple(detector: NoopStreakDetector) -> None:
    result = detector.should_skip()
    assert isinstance(result, tuple), "should_skip must return a tuple, not a bare bool"
    assert len(result) == 2
    skip, reason = result
    assert isinstance(skip, bool)
    assert isinstance(reason, str)


def test_should_skip_false_when_no_state(detector: NoopStreakDetector) -> None:
    skip, reason = detector.should_skip()
    assert skip is False
    assert "streak=0" in reason or "no active" in reason


# --- record_noop / streak logic ---


def test_record_noop_increments_streak(detector: NoopStreakDetector) -> None:
    state = detector.record_noop()
    assert state["streak_count"] == 1


def test_record_noop_sets_backoff_when_threshold_reached(
    detector: NoopStreakDetector,
) -> None:
    detector.record_noop()
    state = detector.record_noop()
    assert state["streak_count"] == 2
    assert "backoff_until" in state


def test_should_skip_true_after_threshold(detector: NoopStreakDetector) -> None:
    detector.record_noop()
    detector.record_noop()
    skip, reason = detector.should_skip()
    assert skip is True
    assert "back-off active" in reason


def test_record_noop_below_threshold_does_not_set_backoff(
    detector: NoopStreakDetector,
) -> None:
    # backoff_n=2, so first NOOP should not trigger backoff
    state = detector.record_noop()
    assert "backoff_until" not in state or not state["backoff_until"]


# --- record_success resets streak ---


def test_record_success_resets_streak(detector: NoopStreakDetector) -> None:
    detector.record_noop()
    detector.record_noop()
    skip, _ = detector.should_skip()
    assert skip is True

    detector.record_success()
    skip, _ = detector.should_skip()
    assert skip is False


def test_record_success_clears_backoff_until(detector: NoopStreakDetector) -> None:
    detector.record_noop()
    detector.record_noop()
    state = detector.record_success()
    assert "backoff_until" not in state
    assert state["streak_count"] == 0


# --- backoff expiry ---


def test_should_skip_false_after_backoff_expires(state_file: Path) -> None:
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps({"streak_count": 3, "backoff_until": past.isoformat()})
    )
    d = NoopStreakDetector(state_path=state_file, backoff_n=2, backoff_minutes=30)
    skip, reason = d.should_skip()
    assert skip is False
    assert "expired" in reason


# --- ENV_DISABLE ---


def test_disable_env_var_bypasses_backoff(
    detector: NoopStreakDetector, monkeypatch: pytest.MonkeyPatch
) -> None:
    detector.record_noop()
    detector.record_noop()
    monkeypatch.setenv("PM_NOOP_STREAK_DISABLE", "1")
    skip, reason = detector.should_skip()
    assert skip is False
    assert "disabled" in reason


# --- status ---


def test_status_includes_config_fields(detector: NoopStreakDetector) -> None:
    s = detector.status()
    assert s["backoff_n"] == 2
    assert s["backoff_minutes"] == 30


# --- atomic write / state persistence ---


def test_state_persists_across_instances(state_file: Path) -> None:
    d1 = NoopStreakDetector(state_path=state_file, backoff_n=3, backoff_minutes=10)
    d1.record_noop()

    d2 = NoopStreakDetector(state_path=state_file, backoff_n=3, backoff_minutes=10)
    s = d2.status()
    assert s["streak_count"] == 1


def test_noop_after_expired_backoff_resets_streak(state_file: Path) -> None:
    """After backoff expires, the first NOOP should start streak from 1, not re-arm."""
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    # Write state as if a prior streak of 2 triggered a now-expired backoff
    state_file.write_text(
        json.dumps({"streak_count": 2, "backoff_until": past.isoformat()})
    )
    d = NoopStreakDetector(state_path=state_file, backoff_n=2, backoff_minutes=30)
    # should_skip says False (expired)
    skip, _ = d.should_skip()
    assert skip is False
    # Record a NOOP — should reset streak to 0 first, then increment to 1
    new_state = d.record_noop()
    assert (
        new_state["streak_count"] == 1
    ), "NOOP after expired backoff must start fresh at streak=1, not re-arm at streak=3"
    assert "backoff_until" not in new_state or not new_state["backoff_until"]


def test_record_noop_in_read_only_dir_does_not_crash(tmp_path: Path) -> None:
    """_file_lock must fail-open (not raise) when the state directory is not writable."""
    import os

    state_file = tmp_path / "pm-noop-streak.json"
    d = NoopStreakDetector(state_path=state_file, backoff_n=2, backoff_minutes=30)
    os.chmod(tmp_path, 0o555)
    try:
        # Neither call should raise — both must fail-open silently
        d.record_noop()
        d.record_success()
    finally:
        os.chmod(tmp_path, 0o755)


def test_state_file_is_valid_json(state_file: Path) -> None:
    d = NoopStreakDetector(state_path=state_file, backoff_n=2, backoff_minutes=5)
    d.record_noop()
    raw = state_file.read_text()
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)


# --- CLI ---


def _cli(*args: str, state_file: Path) -> tuple[int, str]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gptme_runloops.pm_noop_streak",
            "--state-file",
            str(state_file),
            *args,
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stderr + result.stdout


def test_cli_check_exits_0_when_no_state(tmp_path: Path) -> None:
    sf = tmp_path / "streak.json"
    rc, _ = _cli("check", state_file=sf)
    assert rc == 0


def test_cli_record_noop_then_check_exits_10(tmp_path: Path) -> None:
    sf = tmp_path / "streak.json"
    d = NoopStreakDetector(state_path=sf, backoff_n=1, backoff_minutes=30)
    d.record_noop()
    # backoff_n=1 means single NOOP triggers backoff; but CLI uses env defaults
    # so pass --backoff-n 1 explicitly
    rc, _ = _cli("--backoff-n", "1", "check", state_file=sf)
    assert rc == 10


def test_cli_record_success_resets(tmp_path: Path) -> None:
    sf = tmp_path / "streak.json"
    rc, out = _cli("record-noop", state_file=sf)
    assert rc == 0
    assert "streak=1" in out

    rc, out = _cli("record-success", state_file=sf)
    assert rc == 0
    assert "streak reset" in out


def test_cli_status_outputs_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PM_NOOP_STREAK_BACKOFF_N", raising=False)
    monkeypatch.delenv("PM_NOOP_STREAK_BACKOFF_MINUTES", raising=False)
    sf = tmp_path / "streak.json"
    rc, out = _cli("status", state_file=sf)
    assert rc == 0
    parsed = json.loads(out)
    assert "backoff_n" in parsed
    assert parsed["backoff_n"] == DEFAULT_BACKOFF_N
    assert parsed["backoff_minutes"] == DEFAULT_BACKOFF_MINUTES
