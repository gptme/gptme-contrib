"""Tests for gptme_subscription.routing."""

from __future__ import annotations

from pathlib import Path

import pytest
from gptme_subscription.routing import (
    clear_rebalance_state,
    combine_window_pacing_snapshots,
    compute_pacing_snapshot,
    compute_rebalance_hold_seconds,
    compute_window_pacing,
    compute_window_pacing_snapshot,
    load_rebalance_state,
    save_rebalance_state,
)

WEEK = 7 * 24 * 3600


class TestComputePacingSnapshot:
    def test_keeps_positive_gap_for_overuse(self) -> None:
        result = compute_pacing_snapshot(0.8, elapsed_fraction=0.5)
        assert result.pace_gap == pytest.approx(0.3)
        assert result.headroom == pytest.approx(0.2)
        assert result.status == "overusing"

    def test_supports_custom_target_policy(self) -> None:
        result = compute_pacing_snapshot(
            0.5, elapsed_fraction=0.5, target_utilization=0.9
        )
        assert result.target_utilization == pytest.approx(0.45)
        assert result.pace_gap == pytest.approx(0.05)
        assert result.status == "on_track"


class TestComputeWindowPacing:
    def test_on_pace_mid_window(self) -> None:
        """50% used at 50% time elapsed = on track."""
        result = compute_window_pacing(0.5, int(WEEK * 0.5), WEEK)
        assert result is not None
        _, gap, status = result
        assert abs(gap) <= 0.05
        assert status == "on_track"

    def test_on_pace_late_window(self) -> None:
        """95% used with 5% time left = on pace (headroom ≈ 0)."""
        result = compute_window_pacing(0.95, int(WEEK * 0.05), WEEK)
        assert result is not None
        _, gap, status = result
        assert abs(gap) <= 0.05
        assert status == "on_track"

    def test_overusing_in_late_window(self) -> None:
        """95% used with 20% time left = overusing."""
        result = compute_window_pacing(0.95, int(WEEK * 0.2), WEEK)
        assert result is not None
        _, gap, status = result
        assert gap > 0.05
        assert status == "overusing"

    def test_underusing_early_window(self) -> None:
        """10% used at 50% time elapsed = underusing."""
        result = compute_window_pacing(0.1, int(WEEK * 0.5), WEEK)
        assert result is not None
        _, gap, status = result
        assert gap < -0.05
        assert status == "underusing"

    def test_returns_none_for_invalid_inputs(self) -> None:
        assert compute_window_pacing(0.5, 0, WEEK) is None
        assert compute_window_pacing(0.5, 3600, 0) is None

    def test_window_snapshot_exposes_headroom(self) -> None:
        result = compute_window_pacing_snapshot(0.7, int(WEEK * 0.25), WEEK)
        assert result is not None
        assert result.headroom == pytest.approx(0.3)
        assert result.target_utilization == pytest.approx(0.75)

    def test_combine_prefers_most_over_budget_window(self) -> None:
        result = combine_window_pacing_snapshots(
            [(0.2, 3600, 18000), (0.6, 302400, 604800)]
        )
        assert result is not None
        assert result.pace_gap > 0
        assert result.status == "overusing"


class TestComputeRebalanceHoldSeconds:
    def test_zero_overage_returns_min_hold(self) -> None:
        hold = compute_rebalance_hold_seconds(0.0)
        assert hold == 600  # REBALANCE_MIN_HOLD

    def test_negative_overage_returns_min_hold(self) -> None:
        hold = compute_rebalance_hold_seconds(-0.1)
        assert hold == 600

    def test_large_overage_clamped_to_max(self) -> None:
        hold = compute_rebalance_hold_seconds(0.5)
        assert hold == 7200  # REBALANCE_MAX_HOLD

    def test_moderate_overage(self) -> None:
        hold = compute_rebalance_hold_seconds(0.05)
        assert 600 <= hold <= 7200


class TestRebalanceStatePersistence:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        state_path = tmp_path / "rebalance.json"
        decision: dict[str, object] = {
            "switched_to": "alice",
            "switched_from": "bob",
            "reason": "weekly exhausted",
            "switched_at": "2026-05-05T12:00:00+00:00",
            "hold_until": "2026-05-05T14:00:00+00:00",
        }
        save_rebalance_state(state_path, decision)
        assert state_path.exists()
        loaded = load_rebalance_state(state_path)
        assert loaded is not None
        assert loaded["switched_to"] == "alice"
        assert loaded["reason"] == "weekly exhausted"

    def test_load_nonexistent_returns_none(self, tmp_path: Path) -> None:
        result = load_rebalance_state(tmp_path / "nonexistent.json")
        assert result is None

    def test_clear_rebalance_state(self, tmp_path: Path) -> None:
        state_path = tmp_path / "rebalance.json"
        save_rebalance_state(state_path, {"switched_to": "alice"})
        assert state_path.exists()
        clear_rebalance_state(state_path)
        assert not state_path.exists()

    def test_clear_nonexistent_does_not_error(self, tmp_path: Path) -> None:
        clear_rebalance_state(tmp_path / "nonexistent.json")
