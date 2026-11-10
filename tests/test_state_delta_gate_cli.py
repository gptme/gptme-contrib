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

import fcntl
import importlib.util
import json
import sys
from concurrent.futures import ThreadPoolExecutor
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


@pytest.fixture(autouse=True)
def clean_gate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Tests must never target a live caller's state or inherit its bypasses.
    for name in (
        "STATE_DELTA_GATE_FILE",
        "STATE_DELTA_GATE_VERBOSE",
        "GATE_ADDRESSED",
        "GATE_QUOTA_BEHIND_PACE",
        "FORCE_SESSION",
        "GATE_EXPLORE_RATE",
    ):
        monkeypatch.delenv(name, raising=False)


def run(workspace: Path, *args: str) -> int:
    return int(state_delta_gate_cli.main(["--workspace", str(workspace), *args]))


def read_state(workspace: Path) -> dict:
    p = workspace / "state" / "state-delta-gate.json"
    return json.loads(p.read_text()) if p.exists() else {}


def write_state(workspace: Path, **values: object) -> None:
    p = workspace / "state" / "state-delta-gate.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(values) + "\n")


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(state_delta_gate_cli, "_now", lambda: now)


@pytest.mark.parametrize("raw", ['{"noop_only_streak":7', "[]"])
@pytest.mark.parametrize("command", ["record-noop", "record-productive"])
def test_invalid_json_state_is_not_overwritten(
    tmp_path: Path, raw: str, command: str
) -> None:
    state_file = tmp_path / "state" / "state-delta-gate.json"
    state_file.parent.mkdir()
    state_file.write_text(raw)
    assert run(tmp_path, command) == 1
    assert state_file.read_text() == raw
    assert run(tmp_path, "check", "--explore-rate", "0") == PROCEED
    assert run(tmp_path, "status") == 1


@pytest.mark.parametrize("value", ["invalid", "inf", None, [], float("inf")])
def test_invalid_streak_does_not_crash(tmp_path: Path, value: object) -> None:
    write_state(tmp_path, noop_only_streak=value)
    assert run(tmp_path, "check", "--explore-rate", "0") == PROCEED
    assert run(tmp_path, "status") == 0
    assert run(tmp_path, "record-noop") == 0
    assert read_state(tmp_path)["noop_only_streak"] == 1
    write_state(tmp_path, noop_only_streak=value)
    assert run(tmp_path, "record-productive") == 0
    assert read_state(tmp_path)["noop_only_streak"] == 0


@pytest.mark.parametrize("command", ["record-noop", "record-productive"])
def test_record_read_failure_preserves_existing_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    command: str,
) -> None:
    write_state(tmp_path, noop_only_streak=7, preserved="metadata")
    state_file = tmp_path / "state" / "state-delta-gate.json"
    before = state_file.read_bytes()
    original_read = Path.read_text

    def fail(path: Path, *args, **kwargs):
        if path == state_file:
            raise PermissionError("injected read failure")
        return original_read(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", fail)
        assert run(tmp_path, command) != 0
    assert state_file.read_bytes() == before
    err = capsys.readouterr().err
    assert "injected read failure" in err
    assert "recorded" not in err


@pytest.mark.parametrize("command", ["record-noop", "record-productive"])
def test_record_holds_sidecar_lock_through_read_and_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    write_state(tmp_path, noop_only_streak=1)
    state_file = tmp_path / "state" / "state-delta-gate.json"
    lock_file = state_file.with_name(state_file.name + ".lock")
    lock_file.touch()
    original_read = state_delta_gate_cli._read_state
    original_write = state_delta_gate_cli._write_state

    def assert_locked() -> None:
        with lock_file.open("a") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def read(path: Path) -> dict:
        assert_locked()
        return dict(original_read(path))

    def write(path: Path, state: dict) -> None:
        assert_locked()
        original_write(path, state)

    monkeypatch.setattr(state_delta_gate_cli, "_read_state", read)
    monkeypatch.setattr(state_delta_gate_cli, "_write_state", write)
    assert run(tmp_path, command) == 0
    # The lock must be released, but the stable inode must not be unlinked.
    with lock_file.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_concurrent_noops_preserve_every_increment(tmp_path: Path) -> None:
    write_state(tmp_path, noop_only_streak=1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: run(tmp_path, "record-noop"), range(16)))
    assert results == [0] * 16
    assert read_state(tmp_path)["noop_only_streak"] == 17


@pytest.mark.parametrize("command", ["record-noop", "record-productive"])
@pytest.mark.parametrize("failure", ["create", "replace"])
def test_record_failure_returns_nonzero_without_success_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    command: str,
    failure: str,
) -> None:
    write_state(tmp_path, noop_only_streak=3, last_executed_ts="unchanged")
    before = read_state(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("injected write failure")

    if failure == "create":
        monkeypatch.setattr(state_delta_gate_cli.tempfile, "NamedTemporaryFile", fail)
    else:
        monkeypatch.setattr(state_delta_gate_cli.os, "replace", fail)
    assert run(tmp_path, command) != 0
    err = capsys.readouterr().err
    assert "injected write failure" in err
    assert "recorded" not in err
    assert read_state(tmp_path) == before
    assert not list((tmp_path / "state").glob("*.tmp"))


@pytest.mark.parametrize("value", ["", "5%"])
@pytest.mark.parametrize("command", ["record-noop", "record-productive", "status"])
def test_invalid_explore_environment_does_not_break_other_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, command: str
) -> None:
    monkeypatch.setenv("GATE_EXPLORE_RATE", value)
    assert run(tmp_path, command) == 0


@pytest.mark.parametrize("value", ["", "5%"])
def test_explicit_explore_rate_overrides_invalid_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("GATE_EXPLORE_RATE", value)
    assert run(tmp_path, "check", "--explore-rate", "0") == PROCEED


@pytest.mark.parametrize("value", ["", "5%"])
def test_invalid_explore_environment_reports_argument_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    value: str,
) -> None:
    monkeypatch.setenv("GATE_EXPLORE_RATE", value)
    with pytest.raises(SystemExit) as exc:
        run(tmp_path, "check")
    assert exc.value.code == 2
    assert "invalid float value" in capsys.readouterr().err


@pytest.mark.parametrize("value", [123, True, ["timestamp"], {"ts": "bad"}])
@pytest.mark.parametrize("force", [False, True])
def test_non_string_timestamp_does_not_suppress_sessions(
    tmp_path: Path, value: object, force: bool
) -> None:
    write_state(tmp_path, noop_only_streak=1, last_executed_ts=value)
    args = ["check", "--explore-rate", "0"]
    if force:
        args.append("--force")
    assert run(tmp_path, *args) == PROCEED


@pytest.mark.parametrize(
    "timestamp",
    [
        {},
        {"last_executed_ts": "not-a-date"},
        {"last_executed_ts": ""},
        {"last_executed_ts": None},
    ],
)
def test_unknown_timestamp_does_not_lock_out_armed_streak(
    tmp_path: Path, timestamp: dict
) -> None:
    write_state(tmp_path, noop_only_streak=4, **timestamp)
    before = read_state(tmp_path)
    assert run(tmp_path, "check", "--explore-rate", "0") == PROCEED
    assert read_state(tmp_path) == before
    assert run(tmp_path, "record-noop") == 0
    assert read_state(tmp_path)["noop_only_streak"] == 5
    assert run(tmp_path, "check", "--explore-rate", "0") == SKIP


@pytest.mark.parametrize(
    "value", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"]
)
def test_timestamp_utc_overflow_fails_open(tmp_path: Path, value: str) -> None:
    write_state(tmp_path, noop_only_streak=1, last_executed_ts=value)
    assert run(tmp_path, "check", "--force", "--explore-rate", "0") == PROCEED
    assert run(tmp_path, "check", "--explore-rate", "0") == PROCEED


@pytest.mark.parametrize("failure", ["invalid-state", "missing-library"])
def test_fail_open_emits_machine_readable_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    failure: str,
) -> None:
    if failure == "invalid-state":
        state_file = tmp_path / "state" / "state-delta-gate.json"
        state_file.parent.mkdir()
        state_file.write_text("[]")
    else:
        monkeypatch.setitem(sys.modules, "gptme_runloops.gates", None)
    assert run(tmp_path, "check", "--explore-rate", "0") == PROCEED
    captured = capsys.readouterr()
    assert captured.out.strip() == "GATE_CAUSE=error GATE_PROCEED=1"
    assert "state-delta-gate:" in captured.err


@pytest.mark.parametrize("verbose", ["flag", "environment"])
@pytest.mark.parametrize("streak", [0, 1])
def test_verbose_preserves_machine_readable_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    verbose: str,
    streak: int,
) -> None:
    write_state(
        tmp_path,
        noop_only_streak=streak,
        last_executed_ts=state_delta_gate_cli._now().isoformat(),
    )
    args = ["check", "--explore-rate", "0"]
    if verbose == "flag":
        args.insert(0, "--verbose")
    else:
        monkeypatch.setenv("STATE_DELTA_GATE_VERBOSE", "1")
    assert run(tmp_path, *args) == (SKIP if streak else PROCEED)
    captured = capsys.readouterr()
    assert captured.out.strip() == (
        "GATE_CAUSE=skip GATE_PROCEED=0"
        if streak
        else "GATE_CAUSE=inactive GATE_PROCEED=1"
    )
    assert "state-delta-gate:" in captured.err


@pytest.mark.parametrize("command", ["record-noop", "record-productive"])
def test_record_round_trips_escaped_surrogate_metadata(
    tmp_path: Path, command: str
) -> None:
    write_state(tmp_path, noop_only_streak=1, metadata="\ud800")
    assert run(tmp_path, command) == 0
    state = read_state(tmp_path)
    assert state["metadata"] == "\ud800"
    assert state["noop_only_streak"] == (2 if command == "record-noop" else 0)
    assert state["last_executed_ts"] == state_delta_gate_cli._now().isoformat()
    assert not list((tmp_path / "state").glob("*.tmp"))


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
        last_executed_ts=(
            state_delta_gate_cli._now() - timedelta(seconds=10)
        ).isoformat(),
    )
    assert run(tmp_path, "check", "--force", "--explore-rate", "0") == PROCEED


# --- Basic skip: streak armed, backoff not elapsed, not addressed, explore off ---


def test_armed_streak_skips_before_backoff(tmp_path: Path) -> None:
    write_state(
        tmp_path,
        noop_only_streak=1,
        last_executed_ts=state_delta_gate_cli._now().isoformat(),
    )
    assert run(tmp_path, "check", "--explore-rate", "0") == SKIP


def test_armed_streak_proceeds_after_backoff(tmp_path: Path) -> None:
    # streak=1 → backoff=1800s; set last_executed 1801s ago
    write_state(
        tmp_path,
        noop_only_streak=1,
        last_executed_ts=(
            state_delta_gate_cli._now() - timedelta(seconds=1801)
        ).isoformat(),
    )
    assert run(tmp_path, "check", "--explore-rate", "0") == PROCEED


def test_backoff_doubles_per_streak(tmp_path: Path) -> None:
    # streak=2 → backoff=3600s; 3599s elapsed → still skip
    write_state(
        tmp_path,
        noop_only_streak=2,
        last_executed_ts=(
            state_delta_gate_cli._now() - timedelta(seconds=3599)
        ).isoformat(),
    )
    assert run(tmp_path, "check", "--explore-rate", "0") == SKIP

    # 3601s elapsed → proceed
    write_state(
        tmp_path,
        noop_only_streak=2,
        last_executed_ts=(
            state_delta_gate_cli._now() - timedelta(seconds=3601)
        ).isoformat(),
    )
    assert run(tmp_path, "check", "--explore-rate", "0") == PROCEED


def test_backoff_caps_at_4h(tmp_path: Path) -> None:
    # streak=4 → backoff=14400s (4h cap); 14399s elapsed → skip
    write_state(
        tmp_path,
        noop_only_streak=4,
        last_executed_ts=(
            state_delta_gate_cli._now() - timedelta(seconds=14399)
        ).isoformat(),
    )
    assert run(tmp_path, "check", "--explore-rate", "0") == SKIP

    # streak=9 → same 14400s cap; 14401s → proceed
    write_state(
        tmp_path,
        noop_only_streak=9,
        last_executed_ts=(
            state_delta_gate_cli._now() - timedelta(seconds=14401)
        ).isoformat(),
    )
    assert run(tmp_path, "check", "--explore-rate", "0") == PROCEED


# --- Addressed fast path ---


def test_addressed_signal_proceeds_before_backoff(tmp_path: Path) -> None:
    write_state(
        tmp_path,
        noop_only_streak=5,
        last_executed_ts=state_delta_gate_cli._now().isoformat(),
    )
    assert run(tmp_path, "check", "--addressed", "1", "--explore-rate", "0") == PROCEED


def test_addressed_signal_true_values(tmp_path: Path) -> None:
    write_state(
        tmp_path,
        noop_only_streak=3,
        last_executed_ts=state_delta_gate_cli._now().isoformat(),
    )
    for val in ("1", "true", "yes"):
        assert (
            run(tmp_path, "check", "--addressed", val, "--explore-rate", "0") == PROCEED
        )


def test_addressed_false_falls_through(tmp_path: Path) -> None:
    write_state(
        tmp_path,
        noop_only_streak=1,
        last_executed_ts=state_delta_gate_cli._now().isoformat(),
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
        last_executed_ts=state_delta_gate_cli._now().isoformat(),
    )
    assert run(tmp_path, "check", "--explore-rate", "1") == PROCEED


def test_explore_rate_0_never_promotes_skip(tmp_path: Path) -> None:
    write_state(
        tmp_path,
        noop_only_streak=1,
        last_executed_ts=state_delta_gate_cli._now().isoformat(),
    )
    assert run(tmp_path, "check", "--explore-rate", "0") == SKIP


# --- Quota bypass ---


def test_quota_behind_pace_bypasses(tmp_path: Path) -> None:
    write_state(
        tmp_path,
        noop_only_streak=3,
        last_executed_ts=state_delta_gate_cli._now().isoformat(),
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
