"""Tests for aw_data.py — ActivityWatch integration."""

from datetime import date

import pytest

from gptme_activity_summary.aw_data import (
    AppUsage,
    AWActivity,
    fetch_aw_activity,
    format_aw_activity_for_prompt,
)


def test_format_aw_activity_empty():
    """Empty/unavailable activity returns empty string."""
    activity = AWActivity(start_date=date.today(), end_date=date.today(), available=False)
    assert format_aw_activity_for_prompt(activity) == ""


def test_format_aw_activity_no_apps():
    """Available activity with no apps returns empty string."""
    activity = AWActivity(start_date=date.today(), end_date=date.today(), available=True)
    assert format_aw_activity_for_prompt(activity) == ""


def test_format_aw_activity_with_data():
    """Activity with apps formats correctly."""
    activity = AWActivity(
        start_date=date(2026, 2, 28),
        end_date=date(2026, 2, 28),
        available=True,
        total_active_seconds=7200,  # 2 hours
        top_apps=[
            AppUsage(app="nvim", duration=3600),
            AppUsage(app="Firefox", duration=1800),
            AppUsage(app="Terminal", duration=1800),
        ],
    )
    text = format_aw_activity_for_prompt(activity)
    assert "ActivityWatch" in text
    assert "nvim" in text
    assert "Firefox" in text
    assert "Terminal" in text
    assert "2.0h" in text  # total active hours
    assert "2026-02-28" in text


def test_format_aw_activity_percentages():
    """App percentages are calculated correctly."""
    activity = AWActivity(
        start_date=date.today(),
        end_date=date.today(),
        available=True,
        total_active_seconds=3600,
        top_apps=[
            AppUsage(app="AppA", duration=1800),  # 50%
            AppUsage(app="AppB", duration=900),  # 25%
            AppUsage(app="AppC", duration=900),  # 25%
        ],
    )
    text = format_aw_activity_for_prompt(activity)
    assert "50%" in text
    assert "25%" in text


def test_fetch_aw_activity_returns_unavailable_when_no_server():
    """fetch_aw_activity returns empty activity if AW server not running."""
    import os

    # Point to a port that definitely has nothing running
    original_server = os.environ.get("AW_SERVER")
    os.environ["AW_SERVER"] = "http://localhost:59999"

    try:
        from gptme_activity_summary import aw_data

        aw_data.AW_SERVER = "http://localhost:59999"
        activity = fetch_aw_activity(date.today(), date.today())
        assert not activity.available
        assert activity.top_apps == []
        assert activity.total_active_seconds == 0.0
    finally:
        aw_data.AW_SERVER = original_server or "http://localhost:5600"
        if original_server is not None:
            os.environ["AW_SERVER"] = original_server
        elif "AW_SERVER" in os.environ:
            del os.environ["AW_SERVER"]


def test_aw_activity_total_hours():
    """total_active_hours property works correctly."""
    activity = AWActivity(
        start_date=date.today(),
        end_date=date.today(),
        total_active_seconds=5400,  # 1.5 hours
    )
    assert activity.total_active_hours == pytest.approx(1.5)
