"""Contract tests for runtime-admission gates.

These pin the extracted :func:`state_delta_gate` to the exact decision the live
bash chain (`scripts/autonomous-run-cc.sh`, state-delta pre-gate) makes, branch
by branch. The key invariants beyond the proceed/skip decision:

- ``has_changes`` is a *lazy* callable and must only be invoked on the branch
  where the bash chain runs ``state-delta.py --check``. It has side effects
  (persisting the "missing set"), so calling it on a bypass branch would diverge
  from the live behavior.
"""

from gptme_runloops.session_gates import GateDecision, state_delta_gate


class _Spy:
    """A ``has_changes`` stub that records whether it was called."""

    def __init__(self, result: bool) -> None:
        self.result = result
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.result


def test_not_applicable_without_noop_streak():
    """streak < 1 → proceed, and the change check is never run."""
    spy = _Spy(False)
    decision = state_delta_gate(noop_only_streak=0, has_changes=spy)
    assert decision.proceed is True
    assert spy.calls == 0
    assert "not applicable" in decision.reason


def test_force_session_bypasses_gate():
    """force_session → proceed even with a streak and no changes; no check run."""
    spy = _Spy(False)
    decision = state_delta_gate(noop_only_streak=5, has_changes=spy, force_session=True)
    assert decision.proceed is True
    assert spy.calls == 0


def test_quota_behind_pace_bypasses_without_running_check():
    """quota behind pace → proceed, and crucially the change check is NOT run.

    The bash chain skips ``state-delta.py --check`` on this branch, so its
    side effect (persisting the current missing set) must not fire either.
    """
    spy = _Spy(False)
    decision = state_delta_gate(
        noop_only_streak=3, has_changes=spy, quota_behind_pace=True
    )
    assert decision.proceed is True
    assert spy.calls == 0
    assert "quota behind pace" in decision.reason


def test_changes_detected_proceeds():
    """streak >= 1, not behind pace, changes present → proceed (check is run)."""
    spy = _Spy(True)
    decision = state_delta_gate(noop_only_streak=2, has_changes=spy)
    assert decision.proceed is True
    assert spy.calls == 1
    assert "changes detected" in decision.reason


def test_no_changes_skips():
    """streak >= 1, not behind pace, no changes → skip."""
    spy = _Spy(False)
    decision = state_delta_gate(noop_only_streak=4, has_changes=spy)
    assert decision.proceed is False
    assert spy.calls == 1
    assert "no state changes" in decision.reason
    assert "streak: 4" in decision.reason


def test_force_takes_precedence_over_quota_and_changes():
    """force_session short-circuits before quota and the change check."""
    spy = _Spy(True)
    decision = state_delta_gate(
        noop_only_streak=9,
        has_changes=spy,
        force_session=True,
        quota_behind_pace=True,
    )
    assert decision.proceed is True
    assert spy.calls == 0


def test_decision_is_frozen():
    """GateDecision is an immutable value object (safe to log/pass around)."""
    decision = GateDecision(proceed=True, reason="x")
    try:
        decision.proceed = False  # type: ignore[misc]
    except AttributeError:
        pass
    else:  # pragma: no cover - only reached if frozen=True regresses
        raise AssertionError("GateDecision should be frozen")
