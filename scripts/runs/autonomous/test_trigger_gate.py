"""Contract tests for the composable trigger-gate framework.

These pin the semantics that the three existing implementations (contrib's
`session-gate.py`, Gordon's `session-gate.py`, Alice's inline bash chain) agreed
on, so a rebuild on this framework is provably behaviour-preserving:

- durable state fails open (missing/corrupt -> {}), never wedging the gate;
- ``max_skip`` force-runs on no-session and on a stale session — the composite
  fail-open safety valve;
- ``blocked_window`` suppresses only while the window is in the future;
- suppressors hold suppressible triggers but never a non-suppressible one;
- a trigger that raises fails toward skip without crashing the gate.

No network, no subprocess: every trigger here is a pure function of state.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trigger_gate import (  # type: ignore[import-not-found]  # noqa: E402  # resolved at runtime via sys.path (non-package dir)
    UTC,
    GateResult,
    TriggerSpec,
    blocked_window,
    load_state,
    mark_session_ran,
    max_skip_trigger,
    parse_dt,
    run_gate,
    save_state,
)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# --- durable state: fail-open contract -------------------------------------


def test_load_state_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_state(tmp_path / "nope.json") == {}


def test_load_state_corrupt_json_fails_open(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not valid json")
    # Corrupt state must never wedge the gate — empty state lets max_skip run.
    assert load_state(path) == {}


def test_load_state_non_object_fails_open(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("[1, 2, 3]")
    assert load_state(path) == {}


def test_save_then_load_roundtrips(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "state.json"  # parent dir created on save
    state = {"last_session_ts": "2026-09-24T00:00:00+00:00", "blocked_until": None}
    save_state(path, state)
    assert load_state(path) == state


def test_parse_dt_tolerates_z_suffix_and_naive() -> None:
    assert parse_dt("2026-09-24T12:00:00Z") == datetime(2026, 9, 24, 12, tzinfo=UTC)
    # naive assumed UTC
    assert parse_dt("2026-09-24T12:00:00") == datetime(2026, 9, 24, 12, tzinfo=UTC)
    assert parse_dt(None) is None
    assert parse_dt("garbage") is None


# --- max_skip: the force-run floor -----------------------------------------


def test_max_skip_forces_run_when_no_session_recorded() -> None:
    fired, reason = max_skip_trigger(4.0)({})
    assert fired
    assert reason and "no previous session" in reason


def test_max_skip_forces_run_when_stale() -> None:
    old = datetime.now(UTC) - timedelta(hours=5)
    fired, reason = max_skip_trigger(4.0)({"last_session_ts": _iso(old)})
    assert fired
    assert reason and "max skip exceeded" in reason


def test_max_skip_quiet_when_recent() -> None:
    recent = datetime.now(UTC) - timedelta(hours=1)
    fired, _ = max_skip_trigger(4.0)({"last_session_ts": _iso(recent)})
    assert not fired


# --- blocked_window suppressor ---------------------------------------------


def test_blocked_window_inactive_when_key_absent() -> None:
    active, _ = blocked_window()({})
    assert not active


def test_blocked_window_active_while_future() -> None:
    future = datetime.now(UTC) + timedelta(hours=3)
    active, reason = blocked_window()({"blocked_until": _iso(future)})
    assert active
    assert reason and "remaining" in reason


def test_blocked_window_expired_is_inactive() -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    active, _ = blocked_window()({"blocked_until": _iso(past)})
    assert not active


# --- run_gate composition ---------------------------------------------------


def _const(value: bool, reason: str = "r"):
    return lambda state: (value, reason)


def test_run_gate_should_run_iff_a_trigger_fires() -> None:
    quiet = run_gate({}, [TriggerSpec("a", _const(False))])
    assert not quiet.should_run
    assert quiet.reasons == []

    loud = run_gate({}, [TriggerSpec("a", _const(True, "hit"))])
    assert loud.should_run
    assert loud.reasons == [("a", "hit")]


def test_suppressor_holds_suppressible_trigger() -> None:
    result = run_gate(
        {},
        [TriggerSpec("task", _const(True, "has work"), suppressible=True)],
        suppressors=[("overusing", _const(True, "budget"))],
    )
    assert not result.should_run
    assert result.suppressed_by == [("overusing", "budget")]


def test_non_suppressible_trigger_fires_through_suppressor() -> None:
    # The whole point of max_skip / inbox: they must run even while suppressed.
    result = run_gate(
        {},
        [
            TriggerSpec("task", _const(True, "has work"), suppressible=True),
            TriggerSpec("max_skip", _const(True, "floor"), suppressible=False),
        ],
        suppressors=[("overusing", _const(True, "budget"))],
    )
    assert result.should_run
    assert result.reasons == [("max_skip", "floor")]


def test_trigger_that_raises_fails_toward_skip() -> None:
    def boom(state):
        raise RuntimeError("kaboom")

    result = run_gate(
        {},
        [
            TriggerSpec("broken", boom),
            TriggerSpec("ok", _const(True, "still works")),
        ],
    )
    # A broken check must not crash the gate or force a run; the healthy trigger
    # is still evaluated.
    assert result.should_run
    assert result.reasons == [("ok", "still works")]


def test_composite_fails_open_via_max_skip_when_everything_else_breaks() -> None:
    def boom(state):
        raise RuntimeError("kaboom")

    # Every domain trigger is broken AND a suppressor is genuinely active — the
    # gate would go silent forever without the non-suppressible floor. A future
    # blocked_until is required, otherwise blocked_window() is inactive and the
    # test wouldn't actually exercise "max_skip fires *through* a suppressor".
    result = run_gate(
        {"blocked_until": _iso(datetime.now(UTC) + timedelta(hours=1))},
        [
            TriggerSpec("broken", boom, suppressible=True),
            TriggerSpec("max_skip", max_skip_trigger(4.0), suppressible=False),
        ],
        suppressors=[("blocked", blocked_window())],
    )
    assert result.should_run
    assert result.reasons[0][0] == "max_skip"


def test_max_skip_forces_run_on_future_timestamp() -> None:
    # A last_session_ts in the future (clock skew, cross-machine state write, or
    # a corrupt/manual edit) must not silently wedge the floor: hours_since would
    # be negative and the >= check would never fire. max_skip is the fail-open
    # valve, so a future timestamp forces a run.
    future = datetime.now(UTC) + timedelta(hours=5)
    fired, reason = max_skip_trigger(4.0)({"last_session_ts": _iso(future)})
    assert fired
    assert "future" in reason


# --- audit trail & observe/commit split ------------------------------------


def test_apply_to_state_records_audit_without_advancing_session() -> None:
    state: dict = {}
    result = GateResult(should_run=True, reasons=[("inbox", "unread")])
    result.apply_to_state(state)
    assert state["last_decision"] == "run"
    assert state["last_reasons"] == [{"key": "inbox", "reason": "unread"}]
    # Evaluation must not advance last_session_ts — only mark_session_ran does.
    assert "last_session_ts" not in state


def test_mark_session_ran_advances_timestamp() -> None:
    state: dict = {}
    mark_session_ran(state)
    assert parse_dt(state["last_session_ts"]) is not None
