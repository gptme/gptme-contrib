"""Tests for gptme_subscription.routing."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from gptme_subscription.routing import (
    capacity_aware_fallback_order,
    clear_rebalance_state,
    combine_window_pacing_snapshots,
    compute_pacing_snapshot,
    compute_rebalance_hold_seconds,
    compute_window_pacing,
    compute_window_pacing_snapshot,
    load_rebalance_state,
    save_rebalance_state,
    soonest_resetting_fallback,
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

    def test_load_empty_file_returns_none(self, tmp_path: Path) -> None:
        state_path = tmp_path / "rebalance.json"
        state_path.write_text("")
        assert load_rebalance_state(state_path) is None

    def test_load_invalid_json_returns_none(self, tmp_path: Path) -> None:
        state_path = tmp_path / "rebalance.json"
        state_path.write_text("{not valid json")
        assert load_rebalance_state(state_path) is None

    def test_load_non_dict_json_returns_none(self, tmp_path: Path) -> None:
        state_path = tmp_path / "rebalance.json"
        state_path.write_text("[1, 2, 3]")
        assert load_rebalance_state(state_path) is None


class TestComputePacingSnapshotEdgeCases:
    def test_zero_utilization_is_underusing(self) -> None:
        result = compute_pacing_snapshot(0.0, elapsed_fraction=0.5)
        assert result.utilization == pytest.approx(0.0)
        assert result.headroom == pytest.approx(1.0)
        assert result.status == "underusing"

    def test_full_utilization_at_end_is_on_track(self) -> None:
        """100% used at 100% elapsed = on track (gap = 0)."""
        result = compute_pacing_snapshot(1.0, elapsed_fraction=1.0)
        assert result.pace_gap == pytest.approx(0.0)
        assert result.status == "on_track"

    def test_clamping_above_one(self) -> None:
        """Values above 1.0 are clamped to 1.0."""
        result = compute_pacing_snapshot(1.5, elapsed_fraction=0.5)
        assert result.utilization == pytest.approx(1.0)
        assert result.headroom == pytest.approx(0.0)

    def test_at_positive_threshold_boundary_is_overusing(self) -> None:
        """Gap just above threshold (default 0.05) is overusing."""
        result = compute_pacing_snapshot(0.6, elapsed_fraction=0.5, threshold=0.05)
        assert result.pace_gap == pytest.approx(0.1)
        assert result.status == "overusing"


def _write_obs(obs_dir: Path, sub: str, metric_key: str, reset_ts: str) -> None:
    """Write a minimal observation JSON file for testing."""
    obs_dir.mkdir(parents=True, exist_ok=True)
    (obs_dir / f"{sub}.json").write_text(
        json.dumps({"track_resets": {metric_key: reset_ts}}) + "\n"
    )


class TestCapacityAwareFallbackOrder:
    def test_no_observations_preserves_input_order(self, tmp_path: Path) -> None:
        """When no observation files exist, all subs get unknown_pressure and
        the stable sort preserves the original input order."""
        order = ["alice", "bob", "erik"]
        result = capacity_aware_fallback_order(order, tmp_path)
        assert result == order

    def test_recently_reset_sub_sorted_first(self, tmp_path: Path) -> None:
        """A sub that reset recently (low elapsed time → low pressure) sorts before
        one that reset a long time ago (high elapsed → higher pressure)."""
        now = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
        # alice reset 1 day ago → 6 days remaining → low pressure
        _write_obs(
            tmp_path, "alice", "seven_day", (now - timedelta(days=1)).isoformat()
        )
        # bob reset 6 days ago → 1 day remaining → high pressure
        _write_obs(tmp_path, "bob", "seven_day", (now - timedelta(days=6)).isoformat())

        result = capacity_aware_fallback_order(["bob", "alice"], tmp_path, now=now)
        assert result[0] == "alice", (
            f"alice (low pressure) should sort first; got {result}"
        )
        assert result[1] == "bob"

    def test_unknown_sub_ranks_by_unknown_pressure(self, tmp_path: Path) -> None:
        """A sub with no observation file gets unknown_pressure (default 0.5)."""
        now = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
        # alice reset 5 days ago → pressure ~5/7 ≈ 0.71 > 0.5 (unknown)
        _write_obs(
            tmp_path, "alice", "seven_day", (now - timedelta(days=5)).isoformat()
        )
        # bob has no observation → gets unknown_pressure = 0.5

        result = capacity_aware_fallback_order(["alice", "bob"], tmp_path, now=now)
        assert result[0] == "bob", (
            "bob (unknown=0.5) < alice (~0.71); should sort first"
        )


class TestSoonestResettingFallback:
    def test_returns_none_with_no_observations(self, tmp_path: Path) -> None:
        result = soonest_resetting_fallback("bob", ["alice", "erik"], tmp_path)
        assert result is None

    def test_picks_sub_with_least_remaining_time(self, tmp_path: Path) -> None:
        """The sub whose next reset is closest in time (soonest remaining) wins."""
        now = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
        # alice: reset 6.9 days ago → resets in ~0.1 days (8640s)
        _write_obs(
            tmp_path,
            "alice",
            "seven_day",
            (now - timedelta(days=6, hours=21, minutes=36)).isoformat(),
        )
        # erik: reset 6.0 days ago → resets in ~1 day (86400s)
        _write_obs(
            tmp_path,
            "erik",
            "seven_day",
            (now - timedelta(days=6)).isoformat(),
        )

        result = soonest_resetting_fallback("bob", ["alice", "erik"], tmp_path, now=now)
        assert result == "alice", f"alice resets soonest; got {result}"

    def test_skips_active_subscription(self, tmp_path: Path) -> None:
        """The active subscription is never returned even if it would reset soonest."""
        now = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
        # bob (active): reset 6.9 days ago → soonest reset
        _write_obs(
            tmp_path,
            "bob",
            "seven_day",
            (now - timedelta(days=6, hours=23)).isoformat(),
        )
        # alice: reset 4 days ago → resets in 3 days
        _write_obs(
            tmp_path,
            "alice",
            "seven_day",
            (now - timedelta(days=4)).isoformat(),
        )

        result = soonest_resetting_fallback("bob", ["bob", "alice"], tmp_path, now=now)
        assert result == "alice", f"active sub 'bob' must be skipped; got {result}"
