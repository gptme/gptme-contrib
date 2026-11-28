"""Pressure scoring in the manager calls subscription_pressure_from_usage()
with the observation module's hard-coded defaults instead of Config's
thresholds, so a slot that Config considers healthy is scored as exhausted."""

from __future__ import annotations

from datetime import datetime, timezone


def test_capacity_rebalance_uses_configured_sonnet_threshold(make_manager) -> None:
    sm = make_manager()  # Config default sonnet_weekly_exhausted = 0.95
    assert sm.config.sonnet_weekly_exhausted == 0.95
    now = datetime.now(timezone.utc)
    # Fallback erik observed at 65% weekly.
    sm.record_sub_reset_time(
        "erik",
        3 * 24 * 3600,
        {"seven_day": {"utilization": 0.65}, "seven_day_sonnet": {"utilization": 0.10}},
    )
    # Active fallback alice: Sonnet 88% -- below the configured 95% exhaustion
    # threshold, so pressure should be 0.88 and the margin to erik (0.23) is
    # below capacity_rebalance_margin (0.25): no capacity rebalance.
    alice_usage = {
        "seven_day": {"utilization": 0.30, "resets_in_seconds": 3 * 24 * 3600},
        "seven_day_sonnet": {"utilization": 0.88, "resets_in_seconds": 3 * 24 * 3600},
    }
    blocked, _ = sm.is_subscription_blocked(alice_usage, config=sm.config)
    assert not blocked
    result = sm.best_lower_pressure_fallback("alice", alice_usage, now=now)
    assert result is None, f"scored alice as exhausted under default 0.85: {result}"
