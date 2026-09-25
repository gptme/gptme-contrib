"""Contract tests for composable runtime-admission gates.

These pin the decision table of :func:`gptme_runloops.gates.state_delta_gate` to
the reference ``autonomous-run-cc.sh`` state-delta pre-gate, so the eventual cut-over
from bash to this function is provably behaviour-preserving. Each case below maps
to one branch of the bash::

    if [ "${FORCE_SESSION:-0}" != "1" ] && [ "$NOOP_ONLY_STREAK" -ge 1 ]; then
        if [ "${QUOTA_BEHIND_PACE:-0}" = "1" ]; then   # -> proceed (bypass)
        elif state-delta.py --check;                   # -> proceed (changes)
        else                                           # -> skip
    fi                                                 # else -> proceed (inactive)
"""

from __future__ import annotations

import pytest
from gptme_runloops.gates import GateDecision, state_delta_gate


def _never() -> bool:
    raise AssertionError("changes_detected must not be called on this path")


class TestStateDeltaGate:
    def test_inactive_when_streak_zero(self) -> None:
        d = state_delta_gate(
            noop_only_streak=0, quota_behind_pace=False, changes_detected=_never
        )
        assert d == GateDecision(True, "gate inactive (no noop-only streak)")

    def test_inactive_when_forced_even_mid_streak(self) -> None:
        # FORCE_SESSION=1 short-circuits before the streak check in the bash.
        d = state_delta_gate(
            noop_only_streak=5,
            quota_behind_pace=False,
            changes_detected=_never,
            force_session=True,
        )
        assert d.proceed is True
        assert "inactive" in d.reason

    def test_quota_bypass_skips_the_change_probe(self) -> None:
        # The elif is never reached when quota is behind pace: changes_detected
        # must not be invoked (matches bash short-circuit / cost saving).
        d = state_delta_gate(
            noop_only_streak=2, quota_behind_pace=True, changes_detected=_never
        )
        assert d.proceed is True
        assert "quota behind pace" in d.reason

    def test_armed_and_changes_detected_proceeds(self) -> None:
        d = state_delta_gate(
            noop_only_streak=1,
            quota_behind_pace=False,
            changes_detected=lambda: True,
        )
        assert d.proceed is True
        assert "changes detected" in d.reason

    def test_armed_no_changes_skips(self) -> None:
        d = state_delta_gate(
            noop_only_streak=3,
            quota_behind_pace=False,
            changes_detected=lambda: False,
        )
        assert d.proceed is False
        assert "no state changes" in d.reason
        assert "noop-only streak: 3" in d.reason  # streak echoed for the log line

    def test_change_probe_called_at_most_once(self) -> None:
        calls = {"n": 0}

        def probe() -> bool:
            calls["n"] += 1
            return False

        state_delta_gate(
            noop_only_streak=1, quota_behind_pace=False, changes_detected=probe
        )
        assert calls["n"] == 1

    @pytest.mark.parametrize(
        "streak,quota,changes,force,expected_proceed",
        [
            # Full truth table over the four inputs (changes only matters when armed
            # and not bypassed). Mirrors the bash decision tree exactly.
            (0, False, False, False, True),  # inactive
            (0, True, False, False, True),  # inactive (streak dominates)
            (1, False, False, False, False),  # armed, no change -> skip
            (1, False, True, False, True),  # armed, change -> proceed
            (1, True, False, False, True),  # armed, quota bypass -> proceed
            (2, False, False, True, True),  # forced -> inactive
        ],
    )
    def test_truth_table(
        self,
        streak: int,
        quota: bool,
        changes: bool,
        force: bool,
        expected_proceed: bool,
    ) -> None:
        d = state_delta_gate(
            noop_only_streak=streak,
            quota_behind_pace=quota,
            changes_detected=lambda: changes,
            force_session=force,
        )
        assert d.proceed is expected_proceed
