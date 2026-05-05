"""Tests for subscription_manager.observation."""

from datetime import datetime, timezone

from subscription_manager.observation import (
    _remaining_until_observed_reset,
    load_sub_observations,
    record_sub_reset_time,
)


class TestRemainingUntilObservedReset:
    def test_valid_entry(self):
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
        entry = {
            "observed_at": "2026-05-05T10:00:00+00:00",
            "resets_in_seconds": 10800,  # 3 hours from observation → 13:00
        }
        remaining = _remaining_until_observed_reset(entry, now)
        assert remaining == 3600  # 1 hour left

    def test_already_past(self):
        now = datetime(2026, 5, 5, 14, 0, 0, tzinfo=timezone.utc)
        entry = {
            "observed_at": "2026-05-05T10:00:00+00:00",
            "resets_in_seconds": 10800,
        }
        assert _remaining_until_observed_reset(entry, now) is None

    def test_missing_fields(self):
        now = datetime.now(timezone.utc)
        assert _remaining_until_observed_reset({}, now) is None
        assert _remaining_until_observed_reset({"observed_at": "bad"}, now) is None


class TestRecordAndLoadSubObservations:
    def test_record_and_load(self, tmp_path):
        reset_path = tmp_path / "reset-times.json"
        now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)

        record_sub_reset_time("alice", 86400, reset_path, now=now)

        observations = load_sub_observations(reset_path)
        assert "alice" in observations
        assert observations["alice"]["resets_in_seconds"] == 86400
        assert observations["alice"]["observed_at"] == "2026-05-05T12:00:00+00:00"

    def test_record_with_usage(self, tmp_path):
        reset_path = tmp_path / "reset-times.json"
        usage = {
            "seven_day": {"utilization": 0.75},
            "five_hour": {"utilization": 0.50, "resets_in_seconds": 3600},
            "seven_day_sonnet": {"utilization": 0.80},
        }

        record_sub_reset_time("alice", 86400, reset_path, usage=usage)

        observations = load_sub_observations(reset_path)
        entry = observations["alice"]
        assert entry["weekly_utilization"] == 0.75
        assert entry["five_hour_utilization"] == 0.50
        assert entry["sonnet_weekly_utilization"] == 0.80
        assert entry["five_hour_resets_in_seconds"] == 3600.0
        assert "pressure" in entry

    def test_multiple_subs(self, tmp_path):
        reset_path = tmp_path / "reset-times.json"

        record_sub_reset_time("alice", 86400, reset_path)
        record_sub_reset_time("erik", 43200, reset_path)

        observations = load_sub_observations(reset_path)
        assert len(observations) == 2
        assert observations["alice"]["resets_in_seconds"] == 86400
        assert observations["erik"]["resets_in_seconds"] == 43200

    def test_overwrite_existing(self, tmp_path):
        reset_path = tmp_path / "reset-times.json"

        record_sub_reset_time("alice", 86400, reset_path)
        record_sub_reset_time("alice", 3600, reset_path)

        observations = load_sub_observations(reset_path)
        assert len(observations) == 1
        assert observations["alice"]["resets_in_seconds"] == 3600

    def test_empty_file(self, tmp_path):
        reset_path = tmp_path / "reset-times.json"
        observations = load_sub_observations(reset_path)
        assert observations == {}

    def test_corrupt_file(self, tmp_path):
        reset_path = tmp_path / "reset-times.json"
        reset_path.write_text("not json")
        observations = load_sub_observations(reset_path)
        assert observations == {}
