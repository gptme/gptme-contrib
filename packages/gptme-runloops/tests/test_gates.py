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
    Breaker,
    CircuitBreakerDecision,
    GateDecision,
    StreakCooldownDecision,
    UtilizationBypassDecision,
    failure_circuit_breaker_gate,
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

    def test_pace_gap_repr_rounds_to_reference_three_decimals(self) -> None:
        # A gap with >3 decimals must render byte-identically to the reference's
        # print(round(gap, 3)) — not the raw float. Guards the docstring contract.
        d = utilization_bypass_gate(0.123456, _never_hours)
        assert d.pace_gap_repr == "0.123"
        assert d.pace_gap_repr == f"{round(0.123456, 3)}"

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

    def test_throwing_idle_probe_is_tolerated_as_unknown(self) -> None:
        # A failed idle probe must not propagate — this is the auth-outage path
        # the fallback exists for. The reference bash treats an empty HOURS_IDLE
        # as the else branch (not behind pace, gap "unknown"); mirror that.
        def boom() -> float | None:
            raise RuntimeError("idle probe failed")

        d = utilization_bypass_gate(None, boom)
        assert d.behind_pace is False
        assert d.source == "unknown"
        assert d.pace_gap_repr == "unknown"
        assert d.message is None

    def test_fractional_threshold_not_truncated_in_message(self) -> None:
        # ':g' keeps a fractional threshold honest: the log must not say ">12h"
        # while the comparison uses 12.5 (that was the ':.0f' truncation bug).
        d = utilization_bypass_gate(None, lambda: 13.0, idle_hours_threshold=12.5)
        assert d.behind_pace is True
        assert d.message is not None
        assert "(>12.5h)" in d.message


# Reference breakers from autonomous-run-cc.sh: auth (3 fails -> 2h) then generic
# (5 fails -> 1h). Helpers build them at a given count/age so the tests read like
# the incidents they guard against.
AUTH_COOLDOWN = 7200.0
GENERIC_COOLDOWN = 3600.0


def _auth(count: int, age_seconds: float) -> Breaker:
    return Breaker(
        label="auth-guard",
        count=count,
        age_seconds=age_seconds,
        count_threshold=3,
        cooldown_seconds=AUTH_COOLDOWN,
        failure_noun="auth failures",
        expired_note="Auth failure cooldown expired",
    )


def _generic(count: int, age_seconds: float) -> Breaker:
    return Breaker(
        label="fail-guard",
        count=count,
        age_seconds=age_seconds,
        count_threshold=5,
        cooldown_seconds=GENERIC_COOLDOWN,
        failure_noun="failures",
        expired_note="Generic failure cooldown expired",
    )


class TestFailureCircuitBreakerGate:
    def test_inactive_when_forced(self) -> None:
        # FORCE_SESSION=1 short-circuits the whole breaker block in the bash.
        d = failure_circuit_breaker_gate(
            [_auth(9, 60.0), _generic(9, 60.0)], force_session=True
        )
        assert d == CircuitBreakerDecision(True, "gate inactive (forced session)")

    def test_proceed_when_all_under_threshold(self) -> None:
        # count < threshold never engages, regardless of age.
        d = failure_circuit_breaker_gate([_auth(2, 0.0), _generic(4, 0.0)])
        assert d.proceed is True
        assert d.reset_labels == ()
        assert d.expired_reasons == ()

    def test_auth_breaker_cools_down_first(self) -> None:
        # Auth engaged and within cooldown -> skip; generic never evaluated.
        d = failure_circuit_breaker_gate(
            [_auth(3, 3600.0), _generic(9, 0.0)]  # generic would also block
        )
        assert d.proceed is False
        assert d.reason == (
            "[auth-guard] 3 consecutive auth failures — cooling down (60min remaining)"
        )

    def test_generic_breaker_message_and_floored_remaining(self) -> None:
        # 3600 - 61 = 3539s -> 58.98min -> floored to 58, matching bash integer div.
        d = failure_circuit_breaker_gate([_auth(0, 0.0), _generic(5, 61.0)])
        assert d.proceed is False
        assert d.reason == (
            "[fail-guard] 5 consecutive failures — cooling down (58min remaining)"
        )

    def test_expired_breaker_resets_and_falls_through(self) -> None:
        # Auth engaged but cooldown expired -> reset auth, proceed (generic clear).
        d = failure_circuit_breaker_gate(
            [_auth(3, AUTH_COOLDOWN + 1), _generic(0, 0.0)]
        )
        assert d.proceed is True
        assert d.reset_labels == ("auth-guard",)
        assert d.expired_reasons == (
            "[auth-guard] Auth failure cooldown expired — retrying",
        )

    def test_earlier_expiry_still_resets_when_later_breaker_blocks(self) -> None:
        # Bash falls through: auth expires (rm + echo) THEN generic blocks (exit 0).
        # Both effects must survive.
        d = failure_circuit_breaker_gate(
            [_auth(3, AUTH_COOLDOWN + 1), _generic(5, 0.0)]
        )
        assert d.proceed is False
        assert "[fail-guard] 5 consecutive failures" in d.reason
        assert d.reset_labels == ("auth-guard",)
        assert d.expired_reasons == (
            "[auth-guard] Auth failure cooldown expired — retrying",
        )

    def test_empty_chain_proceeds(self) -> None:
        d = failure_circuit_breaker_gate([])
        assert d.proceed is True
        assert d.reset_labels == ()

    def test_boundary_age_equal_cooldown_is_expired(self) -> None:
        # age < cooldown blocks; age == cooldown is NOT < -> expired path (bash uses -lt).
        d = failure_circuit_breaker_gate([_generic(5, GENERIC_COOLDOWN)])
        assert d.proceed is True
        assert d.reset_labels == ("fail-guard",)
