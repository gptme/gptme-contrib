"""Contract tests for the composable trigger-gate framework.

These pin the decision table so the eventual cut-over from Gordon's/Alice's inline
trigger gates is provably behaviour-preserving. Every semantic claim in
``triggers.py``'s docstring has a test here.
"""

from __future__ import annotations

import json

from gptme_runloops.triggers import (
    TriggerDecision,
    TriggerSpec,
    TriggerState,
    evaluate,
    in_blocked_window,
    load_state,
    mark_session,
    max_skip_trigger,
    record_decision,
    save_state,
)

# --- helpers ---------------------------------------------------------------


def _fires(reason: str = "fired"):
    return lambda state: (True, reason)


def _quiet():
    return lambda state: (False, None)


def _raises():
    def check(state):
        raise RuntimeError("boom")

    return check


# --- TriggerState round-trip / durability ----------------------------------


def test_state_roundtrips_through_dict():
    state = TriggerState(
        last_session_ts=123.0,
        blocked_until=456.0,
        last_reasons=("inbox: 1 unread",),
        observations={"last_balance": 42},
    )
    assert TriggerState.from_dict(state.to_dict()) == state


def test_from_dict_tolerates_missing_and_null_fields():
    # A fresh/partial JSON must degrade to defaults, not raise.
    assert TriggerState.from_dict({}) == TriggerState()
    assert TriggerState.from_dict({"last_session_ts": None}) == TriggerState()


def test_load_state_missing_file_fails_open(tmp_path):
    # A fresh fork with no state file gets defaults, never an error.
    assert load_state(tmp_path / "nope.json") == TriggerState()


def test_load_state_corrupt_file_fails_open(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json")
    assert load_state(p) == TriggerState()


def test_save_then_load_is_identity(tmp_path):
    p = tmp_path / "sub" / "state.json"  # parent created on save
    state = TriggerState(last_session_ts=9.0, blocked_until=1.0, last_reasons=("x: y",))
    save_state(p, state)
    assert load_state(p) == state


def test_save_state_is_atomic_no_temp_left_behind(tmp_path):
    p = tmp_path / "state.json"
    save_state(p, TriggerState(last_session_ts=1.0))
    assert p.exists()
    assert not (tmp_path / "state.json.tmp").exists()
    # And it is valid JSON on disk (not a torn write).
    json.loads(p.read_text())


# --- blocked window --------------------------------------------------------


def test_in_blocked_window_true_before_and_false_after():
    state = TriggerState(blocked_until=100.0)
    assert in_blocked_window(state, now=99.0) is True
    assert in_blocked_window(state, now=100.0) is False  # boundary is not blocked
    assert in_blocked_window(state, now=101.0) is False


def test_default_state_is_never_blocked():
    assert in_blocked_window(TriggerState(), now=0.0) is False


# --- max_skip floor --------------------------------------------------------


def test_max_skip_fires_when_overdue():
    state = TriggerState(last_session_ts=0.0)
    fired, reason = max_skip_trigger(state, max_skip_hours=6.0, now=6 * 3600)
    assert fired is True
    assert reason is not None and "max_skip" in reason


def test_max_skip_does_not_fire_before_floor():
    state = TriggerState(last_session_ts=0.0)
    fired, reason = max_skip_trigger(state, max_skip_hours=6.0, now=5 * 3600)
    assert fired is False
    assert reason is None


def test_max_skip_boundary_is_inclusive():
    # Exactly at the floor counts as overdue (>=), matching Gordon's semantics.
    state = TriggerState(last_session_ts=0.0)
    fired, _ = max_skip_trigger(state, max_skip_hours=6.0, now=6 * 3600)
    assert fired is True


def test_max_skip_fresh_fork_fires_immediately():
    # last_session_ts == 0 means "never ran" -> long overdue -> fire, never wedged.
    state = TriggerState()
    fired, _ = max_skip_trigger(state, max_skip_hours=6.0, now=6 * 3600 + 1)
    assert fired is True


# --- evaluate: core truth table -------------------------------------------


def test_no_triggers_means_skip():
    d = evaluate([], TriggerState())
    assert d == TriggerDecision(should_run=False, reasons=(), suppressed=())


def test_a_single_fire_runs():
    d = evaluate([TriggerSpec("inbox", _fires("1 unread"))], TriggerState())
    assert d.should_run is True
    assert d.reasons == (("inbox", "1 unread"),)


def test_all_quiet_skips():
    d = evaluate([TriggerSpec("inbox", _quiet())], TriggerState())
    assert d.should_run is False
    assert d.reasons == ()


def test_multiple_fires_accumulate_reasons():
    d = evaluate(
        [
            TriggerSpec("inbox", _fires("1 unread")),
            TriggerSpec("standup", _fires("due")),
            TriggerSpec("quiet", _quiet()),
        ],
        TriggerState(),
    )
    assert d.should_run is True
    assert d.reasons == (("inbox", "1 unread"), ("standup", "due"))


def test_trigger_reason_defaults_to_name_when_none():
    d = evaluate([TriggerSpec("inbox", lambda s: (True, None))], TriggerState())
    assert d.reasons == (("inbox", "inbox"),)


# --- evaluate: fail-closed per check ---------------------------------------


def test_broken_trigger_fails_toward_skip():
    d = evaluate([TriggerSpec("boom", _raises())], TriggerState())
    assert d.should_run is False


def test_broken_trigger_does_not_block_a_healthy_one():
    d = evaluate(
        [TriggerSpec("boom", _raises()), TriggerSpec("inbox", _fires())],
        TriggerState(),
    )
    assert d.should_run is True
    assert d.reasons == (("inbox", "fired"),)


# --- evaluate: suppressor semantics ----------------------------------------


def _suppress(reason: str = "over-pace"):
    return lambda state: (True, reason)


def _allow():
    return lambda state: (False, None)


def test_suppressor_vetoes_suppressible_trigger():
    d = evaluate(
        [TriggerSpec("routine", _fires(), suppressible=True)],
        TriggerState(),
        suppressors=[("quota", _suppress())],
    )
    assert d.should_run is False
    assert d.suppressed == (("quota", "over-pace"),)


def test_suppressor_cannot_veto_non_suppressible_trigger():
    d = evaluate(
        [
            TriggerSpec("routine", _fires("r"), suppressible=True),
            TriggerSpec("inbox", _fires("critical"), suppressible=False),
        ],
        TriggerState(),
        suppressors=[("quota", _suppress())],
    )
    assert d.should_run is True
    assert d.reasons == (("inbox", "critical"),)  # routine vetoed, inbox survives
    assert d.suppressed == (("quota", "over-pace"),)


def test_inactive_suppressor_lets_everything_run():
    d = evaluate(
        [TriggerSpec("routine", _fires("r"), suppressible=True)],
        TriggerState(),
        suppressors=[("quota", _allow())],
    )
    assert d.should_run is True
    assert d.suppressed == ()


def test_broken_suppressor_is_ignored_not_fatal():
    # A raising suppressor must not veto everything (fail-open on the suppressor side).
    def boom(state):
        raise RuntimeError("nope")

    d = evaluate(
        [TriggerSpec("routine", _fires("r"), suppressible=True)],
        TriggerState(),
        suppressors=[("broken", boom)],
    )
    assert d.should_run is True
    assert d.suppressed == ()


def test_max_skip_as_non_suppressible_defeats_suppression():
    # The composite fail-open property: max_skip fires through an active suppressor.
    state = TriggerState(last_session_ts=0.0)
    d = evaluate(
        [
            TriggerSpec("routine", _fires("r"), suppressible=True),
            TriggerSpec(
                "max_skip",
                lambda s: max_skip_trigger(s, max_skip_hours=6.0, now=7 * 3600),
                suppressible=False,
            ),
        ],
        state,
        suppressors=[("blocked", _suppress("in backoff"))],
    )
    assert d.should_run is True
    assert d.reasons[0][0] == "max_skip"


# --- observe-vs-commit split -----------------------------------------------


def test_record_decision_sets_last_reasons_without_advancing_ts():
    state = TriggerState(last_session_ts=5.0)
    d = evaluate([TriggerSpec("inbox", _fires("1 unread"))], state)
    updated = record_decision(state, d)
    assert updated.last_reasons == ("inbox: 1 unread",)
    assert updated.last_session_ts == 5.0  # unchanged — that's mark_session's job


def test_mark_session_advances_ts_without_touching_reasons():
    state = TriggerState(last_reasons=("inbox: x",))
    updated = mark_session(state, now=99.0)
    assert updated.last_session_ts == 99.0
    assert updated.last_reasons == ("inbox: x",)


def test_record_then_mark_is_the_full_lifecycle(tmp_path):
    # A realistic round: load -> evaluate -> record -> (run) -> mark -> save.
    p = tmp_path / "state.json"
    state = load_state(p)
    d = evaluate([TriggerSpec("inbox", _fires("1 unread"))], state)
    assert d.should_run
    state = record_decision(state, d)
    state = mark_session(state, now=1000.0)
    save_state(p, state)
    reloaded = load_state(p)
    assert reloaded.last_session_ts == 1000.0
    assert reloaded.last_reasons == ("inbox: 1 unread",)
