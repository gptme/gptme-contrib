"""Tests for subscription_manager.state."""

from datetime import datetime, timedelta, timezone

from subscription_manager.state import (
    clear_rebalance_state,
    load_rebalance_state,
    save_rebalance_state,
)


class TestRebalanceState:
    def test_save_and_load_active(self, tmp_path):
        state_path = tmp_path / "rebalance.json"
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
        hold_until = now + timedelta(hours=6)

        decision = {
            "mode": "rebalance",
            "hold_until": hold_until.isoformat(),
            "pace_overage": 0.15,
            "target": "alice",
            "reason": "bob ahead of pace",
        }
        save_rebalance_state(decision, state_path)
        assert state_path.exists()

        loaded = load_rebalance_state(state_path, now=now)
        assert loaded is not None
        assert loaded["mode"] == "rebalance"
        assert isinstance(loaded["hold_until"], datetime)

    def test_load_expired(self, tmp_path):
        state_path = tmp_path / "rebalance.json"
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
        past_hold = now - timedelta(hours=1)

        decision = {
            "mode": "rebalance",
            "hold_until": past_hold.isoformat(),
            "pace_overage": 0.15,
            "target": "alice",
            "reason": "expired",
        }
        save_rebalance_state(decision, state_path)

        loaded = load_rebalance_state(state_path, now=now)
        assert loaded is None
        # File should be cleaned up
        assert not state_path.exists()

    def test_clear(self, tmp_path):
        state_path = tmp_path / "rebalance.json"
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
        hold_until = now + timedelta(hours=6)

        decision = {
            "mode": "rebalance",
            "hold_until": hold_until.isoformat(),
        }
        save_rebalance_state(decision, state_path)
        assert state_path.exists()

        clear_rebalance_state(state_path)
        assert not state_path.exists()

    def test_clear_nonexistent(self, tmp_path):
        state_path = tmp_path / "nonexistent.json"
        clear_rebalance_state(state_path)  # Should not raise

    def test_non_routing_mode_not_saved(self, tmp_path):
        """Only rebalance/forward-routing/capacity-rebalance modes are persisted."""
        state_path = tmp_path / "rebalance.json"
        decision = {
            "mode": "stay",
            "hold_until": "2026-05-05T12:00:00+00:00",
        }
        save_rebalance_state(decision, state_path)
        assert not state_path.exists()

    def test_forward_routing_mode_persisted(self, tmp_path):
        state_path = tmp_path / "rebalance.json"
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
        hold_until = now + timedelta(hours=8)

        decision = {
            "mode": "forward-routing",
            "hold_until": hold_until.isoformat(),
            "target": "alice",
            "reason": "spreading quota",
        }
        save_rebalance_state(decision, state_path)
        assert state_path.exists()

        loaded = load_rebalance_state(state_path, now=now)
        assert loaded is not None
        assert loaded["mode"] == "forward-routing"

    def test_corrupt_file(self, tmp_path):
        state_path = tmp_path / "rebalance.json"
        state_path.write_text("{not valid json")

        loaded = load_rebalance_state(state_path, now=datetime.now(timezone.utc))
        assert loaded is None
        assert not state_path.exists()  # Cleaned up
