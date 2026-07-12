"""Tests for bobutils.durations."""

from datetime import timedelta

import pytest
from bobutils.durations import parse_duration


@pytest.mark.parametrize(
    "text,expected",
    [
        ("90s", timedelta(seconds=90)),
        ("45m", timedelta(minutes=45)),
        ("24h", timedelta(hours=24)),
        ("7d", timedelta(days=7)),
        ("2w", timedelta(weeks=2)),
        ("0h", timedelta(0)),
        ("7D", timedelta(days=7)),
        (" 7d ", timedelta(days=7)),
        ("7 d", timedelta(days=7)),
    ],
)
def test_valid(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize(
    "text", ["", "   ", "d", "7x", "7dx", "x7d", "-7d", "7.5h", "7d8h"]
)
def test_invalid_raises(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def test_bare_int_rejected_by_default():
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("30")


def test_bare_int_with_default_unit():
    assert parse_duration("30", default_unit="d") == timedelta(days=30)
    assert parse_duration("30", default_unit="m") == timedelta(minutes=30)


def test_explicit_unit_wins_over_default_unit():
    assert parse_duration("30h", default_unit="d") == timedelta(hours=30)


def test_bad_default_unit():
    with pytest.raises(ValueError, match="default_unit must be one of"):
        parse_duration("30", default_unit="x")
