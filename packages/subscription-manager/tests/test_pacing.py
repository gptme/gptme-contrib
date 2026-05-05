"""Tests for subscription_manager.pacing."""

from subscription_manager.pacing import (
    compute_rebalance_hold_seconds,
    compute_window_pacing,
    format_duration,
)


class TestFormatDuration:
    def test_zero(self):
        assert format_duration(0) == "0m"
        assert format_duration(-1) == "0m"

    def test_minutes(self):
        assert format_duration(60) == "1m"
        assert format_duration(300) == "5m"

    def test_hours(self):
        assert format_duration(3600) == "1h"
        assert format_duration(7200) == "2h"

    def test_hours_and_minutes(self):
        assert format_duration(3660) == "1h01m"
        assert format_duration(5400) == "1h30m"


class TestComputeWindowPacing:
    def test_underusing(self):
        result = compute_window_pacing(0.5, 3600 * 24 * 3, 3600 * 24 * 7)
        assert result is not None
        elapsed, gap, status = result
        assert status == "underusing"
        assert gap < 0

    def test_overusing(self):
        result = compute_window_pacing(0.8, 3600 * 24 * 3, 3600 * 24 * 7)
        assert result is not None
        elapsed, gap, status = result
        assert status == "overusing"
        assert gap > 0

    def test_on_track(self):
        # 50% elapsed, 50% used → right on track
        result = compute_window_pacing(0.5, 3600 * 24 * 3.5, 3600 * 24 * 7)
        assert result is not None
        elapsed, gap, status = result
        assert status == "on_track"

    def test_invalid_inputs(self):
        assert compute_window_pacing(0.5, 0, 100) is None
        assert compute_window_pacing(0.5, 100, 0) is None
        assert compute_window_pacing(0.5, -1, 100) is None

    def test_near_boundary(self):
        # Just barely overusing (gap just above 0.05)
        result = compute_window_pacing(0.60, 3600 * 24 * 3, 3600 * 24 * 7)
        assert result is not None
        _, gap, status = result
        # 0.60 - 0.571... ≈ 0.028 — under threshold
        assert status == "on_track"


class TestComputeRebalanceHoldSeconds:
    def test_no_overage(self):
        result = compute_rebalance_hold_seconds(0.0)
        assert result == 6 * 3600  # min_hold default

    def test_small_overage(self):
        result = compute_rebalance_hold_seconds(0.05)
        assert result >= 6 * 3600
        assert result <= 48 * 3600

    def test_large_overage(self):
        result = compute_rebalance_hold_seconds(0.50)
        assert result > 6 * 3600
        assert result <= 48 * 3600

    def test_custom_thresholds(self):
        result = compute_rebalance_hold_seconds(
            0.10, target_utilization=0.80, min_hold=3600, max_hold=86400
        )
        assert result >= 3600
        assert result <= 86400
