"""Tests for scripts/runs/autonomous/state-delta-gate.py.

Contracts:
- check exits 0 (proceed) or 10 (skip) according to state_delta_gate
- record-noop increments noop_only_streak and updates last_executed_ts
- record-productive resets noop_only_streak to 0 and updates last_executed_ts
- skipped timer ticks (no record-* call) change neither counter
- backoff ladder: streak 1→30m, 2→1h, 3→2h, 4+→4h
- addressed flag bypasses the backoff probe
- --force makes the gate inactive regardless of streak
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "runs"
    / "autonomous"
    / "state-delta-gate.py"
)
SPEC = importlib.util.spec_from_file_location("state_delta_gate_cli", SCRIPT)
assert SPEC and SPEC.loader
state_delta_gate_cli = importlib.util.module_from_spec(SPEC)
sys.modules["state_delta_gate_cli"] = state_delta_gate_cli
SPEC.loader.exec_module(state_delta_gate_cli)

PROCEED = state_delta_gate_cli.PROCEED
SKIP = state_delta_gate_cli.SKIP
UTC = timezone.utc


def run(workspace: Path, *args: str) -> int:
    return int(state_delta_gate_cli.main(["--workspace", str(workspace), *args]))


def read_state(workspace: Path) -> dict:
    p = workspace / "state" / "state-delta-gate.json"
    return json.loads(p.read_text()) if p.exists() else {}


def write_state(workspace: Path, **values: object) -> None:
    p = workspace / "state" / "state-delta-gate.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(values) + "\n")


# --- First run / no-state ---


def test_first_run_proceeds(tmp_path: Path) -> None:
    assert run(tmp_path, "check") == PROCEED


def test_first_run_state_absent_proceeds(tmp_path: Path) -> None:
    assert run(tmp_path, "check", "--explore-rate", "0") == PROCEED


# --- Gate inactive (streak=0) ---


def test_streak_zero_always_proceeds(tmp_path: Path) -> None:
    write_state(tmp_path, noop_only_streak=0)
    assert run(tmp_path, "check", "--explore-rate", "0") == PROCEED


# --- Force bypass ---


def test_force_bypasses_even_on_long_streak(tmp_path: Path) -> None:
    write_state(
        tmp_path,
        noop_only_streak=10,
        last_executed_ts=(datetime.now(UTC) - timedelta(seconds=10)).isoformat(),
    )
    assert run(tmp_path, "check", "--force", "--explore-rate", "0") == PROCEED


# --- Basic skip: streak armed, backoff not elapsed, not addressed, explore off ---


def test_armed_streak_skips_before_backoff(tmp_path: Path) -> None:
    write_state(
        tmp_path,
        noop_only_streak=1,
        last_executed_ts=datetime.now(UTC).isoformat(),
    )
    assert run(tmp_path, "check", "--explore-rate", "0") == SKIP


def test_armed_streak_proceeds_after_backoff(tmp_path: Path) -> None:
    # streak=1 → backoff=1800s; set last_executed 1801s ago
    write_state(
        tmp_path,
        noop_only_streak=1,
        last_executed_ts=(datetime.now(UTC) - timedelta(seconds=1801)).isoformat(),
    )
    assert run(tmp_path, "check", "--explore-rate", "0") == PROCEED


def test_backoff_doubles_per_streak(tmp_path: Path) -> None:
    # streak=2 → backoff=3600s; 3599s elapsed → still skip
    write_state(
        tmp_path,
        noop_only_streak=2,
        last_executed_ts=(datetime.now(UTC) - timedelta(seconds=3599)).isoformat(),
    )
    assert run(tmp_path, "check", "--explore-rate", "0") == SKIP

    # 3601s elapsed → proceed
    write_state(
        tmp_path,
        noop_only_streak=2,
        last_executed_ts=(datetime.now(UTC) - timedelta(seconds=3601)).isoformat(),
    )
    assert run(tmp_path, "check", "--explore-rate", "0") == PROCEED


def test_backoff_caps_at_4h(tmp_path: Path) -> None:
    # streak=4 → backoff=14400s (4h cap); 14399s elapsed → skip
    write_state(
        tmp_path,
        noop_only_streak=4,
        last_executed_ts=(datetime.now(UTC) - timedelta(seconds=14399)).isoformat(),
    )
    assert run(tmp_path, "check", "--explore-rate", "0") == SKIP

    # streak=9 → same 14400s cap; 14401s → proceed
    write_state(
        tmp_path,
        noop_only_streak=9,
        last_executed_ts=(datetime.now(UTC) - timedelta(seconds=14401)).isoformat(),
    )
    assert run(tmp_path, "check", "--explore-rate", "0") == PROCEED


# --- Addressed fast path ---


def test_addressed_signal_proceeds_before_backoff(tmp_path: Path) -> None:
    write_state(
        tmp_path,
        noop_only_streak=5,
        last_executed_ts=datetime.now(UTC).isoformat(),
    )
    assert run(tmp_path, "check", "--addressed", "1", "--explore-rate", "0") == PROCEED


def test_addressed_signal_true_values(tmp_path: Path) -> None:
    write_state(
        tmp_path,
        noop_only_streak=3,
        last_executed_ts=datetime.now(UTC).isoformat(),
    )
    for val in ("1", "true", "yes"):
        assert (
            run(tmp_path, "check", "--addressed", val, "--explore-rate", "0") == PROCEED
        )


def test_addressed_false_falls_through(tmp_path: Path) -> None:
    write_state(
        tmp_path,
        noop_only_streak=1,
        last_executed_ts=datetime.now(UTC).isoformat(),
    )
    assert run(tmp_path, "check", "--addressed", "0", "--explore-rate", "0") == SKIP


# --- record-noop and record-productive ---


def test_record_noop_increments_streak(tmp_path: Path) -> None:
    run(tmp_path, "record-noop")
    state = read_state(tmp_path)
    assert state["noop_only_streak"] == 1
    assert "last_executed_ts" in state


def test_record_noop_twice_increments_to_two(tmp_path: Path) -> None:
    run(tmp_path, "record-noop")
    run(tmp_path, "record-noop")
    assert read_state(tmp_path)["noop_only_streak"] == 2


def test_record_productive_resets_streak(tmp_path: Path) -> None:
    write_state(tmp_path, noop_only_streak=5)
    run(tmp_path, "record-productive")
    state = read_state(tmp_path)
    assert state["noop_only_streak"] == 0
    assert "last_executed_ts" in state


def test_record_productive_after_noops_resets(tmp_path: Path) -> None:
    for _ in range(3):
        run(tmp_path, "record-noop")
    assert read_state(tmp_path)["noop_only_streak"] == 3
    run(tmp_path, "record-productive")
    assert read_state(tmp_path)["noop_only_streak"] == 0


# --- Explore lane (non-deterministic, so test the flag not randomness) ---


def test_explore_rate_1_always_proceeds(tmp_path: Path) -> None:
    write_state(
        tmp_path,
        noop_only_streak=1,
        last_executed_ts=datetime.now(UTC).isoformat(),
    )
    assert run(tmp_path, "check", "--explore-rate", "1") == PROCEED


def test_explore_rate_0_never_promotes_skip(tmp_path: Path) -> None:
    write_state(
        tmp_path,
        noop_only_streak=1,
        last_executed_ts=datetime.now(UTC).isoformat(),
    )
    assert run(tmp_path, "check", "--explore-rate", "0") == SKIP


# --- Quota bypass ---


def test_quota_behind_pace_bypasses(tmp_path: Path) -> None:
    write_state(
        tmp_path,
        noop_only_streak=3,
        last_executed_ts=datetime.now(UTC).isoformat(),
    )
    assert (
        run(tmp_path, "check", "--quota-behind-pace", "1", "--explore-rate", "0")
        == PROCEED
    )


# --- Status subcommand ---


def test_status_prints_valid_json(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    write_state(tmp_path, noop_only_streak=2)
    assert run(tmp_path, "status") == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["noop_only_streak"] == 2
    assert "_derived_backoff_seconds" in data


# --- Custom state file path ---


def test_custom_state_file(tmp_path: Path) -> None:
    custom = tmp_path / "my-gate.json"
    state_delta_gate_cli.main(
        ["--workspace", str(tmp_path), "--state-file", str(custom), "record-noop"]
    )
    assert custom.exists()
    assert json.loads(custom.read_text())["noop_only_streak"] == 1
