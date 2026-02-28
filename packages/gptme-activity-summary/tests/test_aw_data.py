"""Tests for aw_data.py — ActivityWatch integration."""

from datetime import date, datetime, timezone

import pytest

from gptme_activity_summary.aw_data import (
    AppUsage,
    AWActivity,
    BrowserDomain,
    fetch_aw_activity,
    format_aw_activity_for_prompt,
    _build_timeperiod,
    _get_client,
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


def test_format_aw_activity_with_domains():
    """Activity with browser domains includes websites section."""
    activity = AWActivity(
        start_date=date.today(),
        end_date=date.today(),
        available=True,
        total_active_seconds=7200,
        top_apps=[
            AppUsage(app="Firefox", duration=5400),
            AppUsage(app="Terminal", duration=1800),
        ],
        top_domains=[
            BrowserDomain(domain="github.com", duration=3600),
            BrowserDomain(domain="docs.python.org", duration=1800),
        ],
    )
    text = format_aw_activity_for_prompt(activity)
    assert "### Top Websites" in text
    assert "github.com" in text
    assert "docs.python.org" in text


def test_format_aw_activity_domain_minutes():
    """Short domain durations are formatted as minutes, not hours."""
    activity = AWActivity(
        start_date=date.today(),
        end_date=date.today(),
        available=True,
        total_active_seconds=3600,
        top_apps=[AppUsage(app="Firefox", duration=3600)],
        top_domains=[
            BrowserDomain(domain="short-visit.com", duration=120),  # 2 minutes
        ],
    )
    text = format_aw_activity_for_prompt(activity)
    assert "2min" in text


def test_fetch_aw_activity_returns_unavailable_when_no_server():
    """fetch_aw_activity returns empty activity if AW server not running."""
    from gptme_activity_summary import aw_data

    original_server = aw_data.AW_SERVER
    aw_data.AW_SERVER = "http://localhost:59999"

    try:
        activity = fetch_aw_activity(date.today(), date.today())
        assert not activity.available
        assert activity.top_apps == []
        assert activity.total_active_seconds == 0.0
    finally:
        aw_data.AW_SERVER = original_server


def test_aw_activity_total_hours():
    """total_active_hours property works correctly."""
    activity = AWActivity(
        start_date=date.today(),
        end_date=date.today(),
        total_active_seconds=5400,  # 1.5 hours
    )
    assert activity.total_active_hours == pytest.approx(1.5)


def test_build_timeperiod():
    """Time period tuple is built correctly."""
    start_dt, end_dt = _build_timeperiod(date(2026, 2, 28), date(2026, 2, 28))
    assert start_dt == datetime(2026, 2, 28, 0, 0, 0, tzinfo=timezone.utc)
    assert end_dt == datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)  # exclusive end = next day


def test_build_timeperiod_multi_day():
    """Multi-day time period works correctly."""
    start_dt, end_dt = _build_timeperiod(date(2026, 2, 25), date(2026, 2, 28))
    assert start_dt == datetime(2026, 2, 25, 0, 0, 0, tzinfo=timezone.utc)
    assert end_dt == datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)  # day after end date


def test_browser_domain_dataclass():
    """BrowserDomain dataclass works correctly."""
    d = BrowserDomain(domain="github.com", duration=3600)
    assert d.domain == "github.com"
    assert d.duration == 3600


def test_get_client_returns_client():
    """_get_client returns a client instance when aw-client is installed."""
    client = _get_client()
    assert client is not None


def test_get_client_custom_server():
    """_get_client parses custom AW_SERVER."""
    from gptme_activity_summary import aw_data

    original_server = aw_data.AW_SERVER
    aw_data.AW_SERVER = "http://10.0.0.1:5601"

    try:
        client = _get_client()
        assert client is not None
    finally:
        aw_data.AW_SERVER = original_server
