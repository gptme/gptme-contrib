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
from gptme_runloops.gates import (
    GateDecision,
    StreakCooldownDecision,
    UtilizationBypassDecision,
    state_delta_gate,
    streak_cooldown_gate,
    utilization_bypass_gate,
)


def _never() -> bool:
    raise AssertionError("changes_detected must not be called on this path")


def _never_hours() -> float | None:
    raise AssertionError("idle_hours must not be called on the API-available path")


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


# Reference weekday/weekend policy values from autonomous-run-cc.sh, passed in by
# the caller (the gate itself is calendar-agnostic).
WEEKDAY = dict(scaled_threshold=5, toggle_threshold=3, cooldown_multiplier=1)
WEEKEND = dict(scaled_threshold=3, toggle_threshold=2, cooldown_multiplier=2)


class TestStreakCooldownGate:
    def test_inactive_below_toggle_threshold(self) -> None:
        d = streak_cooldown_gate(
            noop_streak=2,
            elapsed_seconds=0,
            quota_behind_pace=False,
            toggle_armed=False,
            **WEEKDAY,
        )
        assert d == StreakCooldownDecision(
            True, "gate inactive (streak below toggle threshold)"
        )

    def test_inactive_when_forced_even_deep_in_streak(self) -> None:
        # FORCE_SESSION=1 short-circuits the whole block in the bash.
        d = streak_cooldown_gate(
            noop_streak=10,
            elapsed_seconds=0,
            quota_behind_pace=False,
            toggle_armed=True,
            force_session=True,
            **WEEKDAY,
        )
        assert d.proceed is True
        assert d.toggle_action is None
        assert "inactive" in d.reason

    def test_toggle_regime_unarmed_proceeds_and_arms(self) -> None:
        d = streak_cooldown_gate(
            noop_streak=3,
            elapsed_seconds=0,
            quota_behind_pace=False,
            toggle_armed=False,
            **WEEKDAY,
        )
        assert d.proceed is True
        assert d.toggle_action == "set"
        assert "attempting" in d.reason

    def test_toggle_regime_armed_skips_and_clears(self) -> None:
        d = streak_cooldown_gate(
            noop_streak=4,
            elapsed_seconds=0,
            quota_behind_pace=False,
            toggle_armed=True,
            **WEEKDAY,
        )
        assert d.proceed is False
        assert d.toggle_action == "clear"
        assert "skipping" in d.reason

    def test_scaled_regime_within_cooldown_skips(self) -> None:
        # streak 5 weekday -> 5h cooldown (18000s); 100s elapsed is inside it.
        d = streak_cooldown_gate(
            noop_streak=5,
            elapsed_seconds=100,
            quota_behind_pace=False,
            toggle_armed=False,
            **WEEKDAY,
        )
        assert d.proceed is False
        assert d.toggle_action is None
        assert "5 consecutive" in d.reason
        assert "100s ago" in d.reason
        assert "5h cooldown" in d.reason

    def test_scaled_regime_quota_bypass_proceeds(self) -> None:
        d = streak_cooldown_gate(
            noop_streak=5,
            elapsed_seconds=100,
            quota_behind_pace=True,
            toggle_armed=False,
            **WEEKDAY,
        )
        assert d.proceed is True
        assert "quota behind pace" in d.reason

    def test_scaled_regime_cooldown_elapsed_proceeds(self) -> None:
        d = streak_cooldown_gate(
            noop_streak=5,
            elapsed_seconds=20000,  # > 18000s (5h)
            quota_behind_pace=False,
            toggle_armed=False,
            **WEEKDAY,
        )
        assert d.proceed is True
        assert "elapsed" in d.reason

    def test_scaled_regime_no_timestamp_proceeds(self) -> None:
        # bash falls through the block when it cannot read a last-session timestamp.
        d = streak_cooldown_gate(
            noop_streak=8,
            elapsed_seconds=None,
            quota_behind_pace=False,
            toggle_armed=False,
            **WEEKDAY,
        )
        assert d.proceed is True
        assert "timestamp" in d.reason

    def test_cooldown_hours_capped_at_12(self) -> None:
        # streak 15 weekday -> min(15, 12) = 12h = 43200s; 40000s is still inside.
        d = streak_cooldown_gate(
            noop_streak=15,
            elapsed_seconds=40000,
            quota_behind_pace=False,
            toggle_armed=False,
            **WEEKDAY,
        )
        assert d.proceed is False
        assert "12h cooldown" in d.reason

    def test_weekend_multiplier_doubles_cooldown(self) -> None:
        # streak 3 weekend -> min(3*2, 12) = 6h = 21600s; 100s inside.
        d = streak_cooldown_gate(
            noop_streak=3,
            elapsed_seconds=100,
            quota_behind_pace=False,
            toggle_armed=False,
            **WEEKEND,
        )
        assert d.proceed is False
        assert "6h cooldown" in d.reason

    def test_weekend_toggle_threshold_lower(self) -> None:
        # streak 2 is inactive on weekday but toggle regime on weekend.
        d = streak_cooldown_gate(
            noop_streak=2,
            elapsed_seconds=0,
            quota_behind_pace=False,
            toggle_armed=False,
            **WEEKEND,
        )
        assert d.proceed is True
        assert d.toggle_action == "set"

    @pytest.mark.parametrize(
        "streak,elapsed,quota,armed,force,exp_proceed,exp_toggle",
        [
            # Weekday policy. Full branch coverage of the decision tree.
            (0, 0, False, False, False, True, None),  # inactive
            (2, 0, False, False, False, True, None),  # inactive (below toggle)
            (3, 0, False, False, False, True, "set"),  # toggle, unarmed -> arm
            (3, 0, False, True, False, False, "clear"),  # toggle, armed -> clear
            (4, 0, True, True, False, False, "clear"),  # toggle ignores quota bypass
            (5, 100, False, False, False, False, None),  # scaled, cooling -> skip
            (5, 100, True, False, False, True, None),  # scaled, quota bypass
            (5, 99999, False, False, False, True, None),  # scaled, elapsed
            (5, None, False, False, False, True, None),  # scaled, no timestamp
            (9, 0, False, True, True, True, None),  # forced -> inactive
        ],
    )
    def test_truth_table_weekday(
        self,
        streak: int,
        elapsed: int | None,
        quota: bool,
        armed: bool,
        force: bool,
        exp_proceed: bool,
        exp_toggle: str | None,
    ) -> None:
        d = streak_cooldown_gate(
            noop_streak=streak,
            elapsed_seconds=elapsed,
            quota_behind_pace=quota,
            toggle_armed=armed,
            force_session=force,
            **WEEKDAY,
        )
        assert d.proceed is exp_proceed
        assert d.toggle_action == exp_toggle


class TestUtilizationBypassGate:
    """Pins the utilisation-bypass block of ``autonomous-run-cc.sh``::

    if [ -n "$QUOTA_PACE_RAW" ]; then                # API available
        QUOTA_BEHIND_PACE = pace_gap > 0.25
    else                                             # scrape unavailable
        if HOURS_IDLE > 12; then QUOTA_BEHIND_PACE=1 # local fallback
        else QUOTA_BEHIND_PACE=0; gap="unknown"
    """

    def test_api_above_threshold_is_behind_pace(self) -> None:
        d = utilization_bypass_gate(0.30, _never_hours)
        assert d == UtilizationBypassDecision(
            behind_pace=True, pace_gap_repr="0.3", source="api", message=None
        )

    def test_api_below_threshold_not_behind_pace(self) -> None:
        d = utilization_bypass_gate(0.10, _never_hours)
        assert d.behind_pace is False
        assert d.source == "api"
        assert d.message is None

    def test_api_at_threshold_not_behind_pace(self) -> None:
        # Strict > 0.25: exactly at the threshold does not bypass (matches bash).
        d = utilization_bypass_gate(0.25, _never_hours)
        assert d.behind_pace is False
        assert d.pace_gap_repr == "0.25"

    def test_idle_probe_skipped_when_api_available(self) -> None:
        # _never_hours raises if called — the API path must not touch the fallback.
        utilization_bypass_gate(0.5, _never_hours)  # no exception == pass

    def test_fallback_long_idle_is_behind_pace(self) -> None:
        d = utilization_bypass_gate(None, lambda: 15.0)
        assert d.behind_pace is True
        assert d.source == "idle_fallback"
        assert d.pace_gap_repr == "unknown (idle 15.00h)"
        assert d.message is not None
        assert "15.00h since last productive session (>12h)" in d.message

    def test_fallback_short_idle_not_behind_pace(self) -> None:
        d = utilization_bypass_gate(None, lambda: 3.0)
        assert d.behind_pace is False
        assert d.source == "unknown"
        assert d.pace_gap_repr == "unknown"
        assert d.message is None

    def test_fallback_at_threshold_not_behind_pace(self) -> None:
        # Strict > 12: exactly 12h idle does not bypass.
        d = utilization_bypass_gate(None, lambda: 12.0)
        assert d.behind_pace is False
        assert d.source == "unknown"

    def test_fallback_idle_unknown_not_behind_pace(self) -> None:
        d = utilization_bypass_gate(None, lambda: None)
        assert d.behind_pace is False
        assert d.source == "unknown"
        assert d.pace_gap_repr == "unknown"

    def test_idle_probe_called_at_most_once(self) -> None:
        calls = {"n": 0}

        def probe() -> float | None:
            calls["n"] += 1
            return 20.0

        utilization_bypass_gate(None, probe)
        assert calls["n"] == 1

    def test_custom_thresholds_honoured_in_decision_and_message(self) -> None:
        # A caller override flows into both the comparison and the log string.
        d = utilization_bypass_gate(None, lambda: 7.0, idle_hours_threshold=6.0)
        assert d.behind_pace is True
        assert d.message is not None
        assert "(>6h)" in d.message

        d2 = utilization_bypass_gate(0.15, _never_hours, pace_gap_threshold=0.10)
        assert d2.behind_pace is True
