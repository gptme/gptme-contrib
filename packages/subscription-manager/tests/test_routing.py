"""Tests for subscription_manager.routing."""

from datetime import datetime, timezone

from subscription_manager.routing import (
    capacity_aware_fallback_order,
    seconds_since_last_switch_to,
    soonest_resetting_fallback,
)


class TestCapacityAwareFallbackOrder:
    def test_basic_ordering(self):
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
        observations = {
            "alice": {
                "observed_at": "2026-05-05T10:00:00+00:00",
                "resets_in_seconds": 7200,  # resets at 12:00
                "pressure": 0.70,
            },
            "erik": {
                "observed_at": "2026-05-05T10:00:00+00:00",
                "resets_in_seconds": 86400,  # resets far away
                "pressure": 0.40,
            },
        }
        order = capacity_aware_fallback_order(["alice", "erik"], observations, now=now)
        # erik has lower pressure → should come first
        assert order[0] == "erik"

    def test_unknown_pressure_defaults_neutral(self):
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
        observations: dict[str, dict] = {}
        order = capacity_aware_fallback_order(["alice", "erik"], observations, now=now)
        # Both unknown → both get 0.50 pressure → tie → original order
        assert order == ["alice", "erik"]


class TestSoonestResettingFallback:
    def test_reset_times_file(self, tmp_path):
        import json

        reset_path = tmp_path / "reset-times.json"
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)

        data = {
            "alice": {
                "observed_at": "2026-05-05T10:00:00+00:00",
                "resets_in_seconds": 3600,  # resets at 11:00 (already past!)
            },
            "erik": {
                "observed_at": "2026-05-05T10:00:00+00:00",
                "resets_in_seconds": 28800,  # resets at 18:00
            },
        }
        reset_path.write_text(json.dumps(data))

        order = soonest_resetting_fallback(["alice", "erik"], reset_path, now=now)
        # alice already reset → inf; erik has 6h → finit
        assert order[0] == "erik"

    def test_unknown_subs_at_end(self, tmp_path):
        import json

        reset_path = tmp_path / "reset-times.json"
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)

        data = {
            "erik": {
                "observed_at": "2026-05-05T10:00:00+00:00",
                "resets_in_seconds": 28800,
            },
        }
        reset_path.write_text(json.dumps(data))

        order = soonest_resetting_fallback(["alice", "erik"], reset_path, now=now)
        # erik has data → first; alice unknown → last
        assert order[0] == "erik"
        assert order[-1] == "alice"


class TestSecondsSinceLastSwitchTo:
    def test_recent_switch(self, tmp_path):
        log_path = tmp_path / "switch.log"
        log_path.write_text(
            "2026-05-05T11:55:00Z switched to bob — quota reset\n"
            "2026-05-04T10:00:00Z switched to alice — bob exhausted\n"
        )

        seconds = seconds_since_last_switch_to(
            "bob",
            log_path,
            now=datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert seconds == 300  # 5 minutes ago

    def test_no_match(self, tmp_path):
        log_path = tmp_path / "switch.log"
        log_path.write_text("2026-05-04T10:00:00Z switched to bob — quota reset\n")

        seconds = seconds_since_last_switch_to(
            "alice",
            log_path,
            now=datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert seconds is None

    def test_no_file(self, tmp_path):
        log_path = tmp_path / "nonexistent.log"
        seconds = seconds_since_last_switch_to("bob", log_path)
        assert seconds is None
