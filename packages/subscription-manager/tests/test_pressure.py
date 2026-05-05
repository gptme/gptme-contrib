"""Tests for subscription_manager.pressure."""

from subscription_manager.pressure import (
    is_subscription_blocked,
    subscription_pressure_from_usage,
)


class TestSubscriptionPressureFromUsage:
    def test_weekly_only(self):
        usage = {
            "seven_day": {"utilization": 0.75},
        }
        assert subscription_pressure_from_usage(usage) == 0.75

    def test_weekly_and_sonnet(self):
        usage = {
            "seven_day": {"utilization": 0.60},
            "seven_day_sonnet": {"utilization": 0.80},
        }
        assert subscription_pressure_from_usage(usage) == 0.80

    def test_five_hour_ignored_when_short_reset(self):
        usage = {
            "seven_day": {"utilization": 0.40},
            "five_hour": {"utilization": 0.95, "resets_in_seconds": 1800},
        }
        # 5h resets in 30min — too short to matter
        assert subscription_pressure_from_usage(usage) == 0.40

    def test_five_hour_counted_when_long_reset(self):
        usage = {
            "seven_day": {"utilization": 0.40},
            "five_hour": {"utilization": 0.95, "resets_in_seconds": 14400},
        }
        # 5h resets in 4h — worth including
        assert subscription_pressure_from_usage(usage) == 0.95

    def test_empty(self):
        assert subscription_pressure_from_usage({}) is None

    def test_no_utilization_keys(self):
        assert subscription_pressure_from_usage({"seven_day": {}}) is None


class TestIsSubscriptionBlocked:
    def test_all_healthy(self):
        usage = {
            "seven_day": {"utilization": 0.50},
            "five_hour": {"utilization": 0.50},
            "seven_day_sonnet": {"utilization": 0.50},
        }
        blocked, reason = is_subscription_blocked(usage)
        assert not blocked
        assert reason == "all limits healthy"

    def test_opus_exhausted(self):
        usage = {
            "seven_day": {"utilization": 0.90},
            "five_hour": {"utilization": 0.95},
            "seven_day_sonnet": {"utilization": 0.50},
        }
        blocked, reason = is_subscription_blocked(usage)
        assert blocked
        assert "Opus exhausted" in reason

    def test_sonnet_exhausted(self):
        usage = {
            "seven_day": {"utilization": 0.50},
            "five_hour": {"utilization": 0.50},
            "seven_day_sonnet": {"utilization": 0.96},
        }
        blocked, reason = is_subscription_blocked(usage)
        assert blocked
        assert "Sonnet exhausted" in reason

    def test_missing_sonnet_data_conservative(self):
        usage = {
            "seven_day": {"utilization": 0.90},
            "five_hour": {"utilization": 0.50},
        }
        blocked, reason = is_subscription_blocked(usage)
        assert blocked
        assert "Sonnet data missing" in reason

    def test_custom_thresholds(self):
        usage = {
            "seven_day": {"utilization": 0.70},
            "five_hour": {"utilization": 0.75},
        }
        blocked, _ = is_subscription_blocked(
            usage, weekly_exhausted=0.65, five_hour_exhausted=0.70
        )
        assert blocked

    def test_sonnet_not_exhausted(self):
        usage = {
            "seven_day": {"utilization": 0.50},
            "five_hour": {"utilization": 0.50},
            "seven_day_sonnet": {"utilization": 0.80},
        }
        blocked, reason = is_subscription_blocked(usage)
        assert not blocked
