"""Tests for gptme_subscription.observation."""

from __future__ import annotations

from pathlib import Path

from gptme_subscription.observation import (
    format_duration,
    is_subscription_blocked,
    load_sub_observations,
    record_sub_reset_time,
    subscription_pressure_from_usage,
)


class TestFormatDuration:
    def test_zero(self) -> None:
        assert format_duration(0) == "0m"

    def test_negative(self) -> None:
        assert format_duration(-100) == "0m"

    def test_minutes_only(self) -> None:
        assert format_duration(1800) == "30m"  # 30 * 60
        assert format_duration(3540) == "59m"  # 59 * 60

    def test_hours_only(self) -> None:
        assert format_duration(3600) == "1h"
        assert format_duration(7200) == "2h"

    def test_hours_and_minutes(self) -> None:
        assert format_duration(3661) == "1h01m"
        assert format_duration(7260) == "2h01m"


class TestIsSubscriptionBlocked:
    def test_healthy_not_blocked(self) -> None:
        usage = {
            "seven_day": {"utilization": 0.3},
            "five_hour": {"utilization": 0.1},
            "seven_day_sonnet": {"utilization": 0.2},
        }
        blocked, reason = is_subscription_blocked(usage)
        assert not blocked
        assert "healthy" in reason

    def test_opus_exhausted_blocked(self) -> None:
        usage = {
            "seven_day": {"utilization": 0.99},
            "five_hour": {"utilization": 0.99},
            "seven_day_sonnet": {"utilization": 0.2},
        }
        blocked, reason = is_subscription_blocked(usage)
        assert blocked
        assert "Opus" in reason

    def test_weekly_high_5h_low_not_opus_blocked(self) -> None:
        usage = {
            "seven_day": {"utilization": 0.99},
            "five_hour": {"utilization": 0.3},
            "seven_day_sonnet": {"utilization": 0.2},
        }
        blocked, _reason = is_subscription_blocked(usage)
        assert not blocked

    def test_sonnet_exhausted_blocked(self) -> None:
        usage = {
            "seven_day": {"utilization": 0.3},
            "five_hour": {"utilization": 0.1},
            "seven_day_sonnet": {"utilization": 0.99},
        }
        blocked, reason = is_subscription_blocked(usage)
        assert blocked
        assert "Sonnet" in reason

    def test_sonnet_missing_weekly_high_conservative_block(self) -> None:
        usage = {
            "seven_day": {"utilization": 0.99},
            "five_hour": {"utilization": 0.99},
        }
        blocked, reason = is_subscription_blocked(usage)
        assert blocked
        assert "Sonnet data missing" in reason

    def test_sonnet_missing_weekly_low_not_blocked(self) -> None:
        usage = {
            "seven_day": {"utilization": 0.3},
            "five_hour": {"utilization": 0.1},
        }
        blocked, _reason = is_subscription_blocked(usage)
        assert not blocked

    def test_both_opus_and_sonnet_exhausted(self) -> None:
        usage = {
            "seven_day": {"utilization": 0.99},
            "five_hour": {"utilization": 0.99},
            "seven_day_sonnet": {"utilization": 0.99},
        }
        blocked, reason = is_subscription_blocked(usage)
        assert blocked
        assert "Opus" in reason
        assert "Sonnet" in reason


class TestSubscriptionPressureFromUsage:
    def test_weekly_only(self) -> None:
        usage = {"seven_day": {"utilization": 0.75}}
        score = subscription_pressure_from_usage(usage)
        assert score is not None
        assert score == 0.75

    def test_weekly_and_sonnet(self) -> None:
        usage = {
            "seven_day": {"utilization": 0.6},
            "seven_day_sonnet": {"utilization": 0.8},
        }
        score = subscription_pressure_from_usage(usage)
        assert score is not None
        assert score == 0.8  # max of components

    def test_5h_within_short_reset_window_excluded(self) -> None:
        usage = {
            "seven_day": {"utilization": 0.3},
            "five_hour": {"utilization": 0.9, "resets_in_seconds": 600},
        }
        score = subscription_pressure_from_usage(usage)
        assert score is not None
        assert score == 0.3  # 5h excluded, only weekly counts

    def test_5h_past_short_reset_window_included(self) -> None:
        usage = {
            "seven_day": {"utilization": 0.3},
            "five_hour": {"utilization": 0.9, "resets_in_seconds": 7201},
        }
        score = subscription_pressure_from_usage(usage)
        assert score is not None
        assert score == 0.9  # max of 0.3 and 0.9

    def test_weekly_threshold_override_treats_value_as_exhausted(self) -> None:
        usage = {"seven_day": {"utilization": 0.8}}
        score = subscription_pressure_from_usage(usage, weekly_exhausted=0.8)
        assert score == 1.0

    def test_5h_threshold_override_treats_value_as_exhausted(self) -> None:
        usage = {
            "seven_day": {"utilization": 0.1},
            "five_hour": {"utilization": 0.7, "resets_in_seconds": 7201},
        }
        score = subscription_pressure_from_usage(usage, five_hour_threshold=0.7)
        assert score == 1.0

    def test_no_usage_data_returns_none(self) -> None:
        usage: dict = {}
        score = subscription_pressure_from_usage(usage)
        assert score is None

    def test_sonnet_weekly_uses_separate_threshold(self) -> None:
        # Sonnet at 90% — above sonnet_weekly_exhausted (0.85) but below weekly_exhausted (0.95).
        # Should report pressure=1.0, not 0.9 (regression: was using wrong threshold).
        usage = {"seven_day_sonnet": {"utilization": 0.90}}
        score = subscription_pressure_from_usage(usage)
        assert score == 1.0

    def test_sonnet_weekly_threshold_override(self) -> None:
        usage = {"seven_day_sonnet": {"utilization": 0.75}}
        score = subscription_pressure_from_usage(usage, sonnet_weekly_exhausted=0.75)
        assert score == 1.0


class TestLoadSubObservations:
    def test_skips_non_dict_json(self, tmp_path: Path) -> None:
        # A valid JSON array is not a dict — should be skipped, not crash
        (tmp_path / "bob.json").write_text("[1, 2, 3]")
        (tmp_path / "alice.json").write_text(
            '{"track_resets": {"seven_day": "2026-01-01T00:00:00+00:00"}}'
        )
        result = load_sub_observations(tmp_path)
        assert "bob" not in result  # skipped
        assert "alice" in result  # valid dict still loaded


class TestRecordSubResetTime:
    def test_writes_observation(self, tmp_path: Path) -> None:
        record_sub_reset_time(tmp_path, "bob", "seven_day", "2026-01-01T00:00:00+00:00")
        data = (tmp_path / "bob.json").read_text()
        assert "seven_day" in data

    def test_corrupt_file_does_not_raise(self, tmp_path: Path) -> None:
        obs_file = tmp_path / "bob.json"
        obs_file.write_text("CORRUPT {{{")
        # Should not raise — corrupt file is silently reset
        record_sub_reset_time(tmp_path, "bob", "seven_day")
        import json

        data = json.loads(obs_file.read_text())
        assert "track_resets" in data
        assert "seven_day" in data["track_resets"]
