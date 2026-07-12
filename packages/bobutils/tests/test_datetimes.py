"""Tests for bobutils.datetimes.parse_datetime."""

from __future__ import annotations

from datetime import UTC, datetime, timezone

from bobutils.datetimes import parse_datetime

# --- None / invalid input ---


def test_none_returns_none():
    assert parse_datetime(None) is None


def test_empty_string_returns_none():
    assert parse_datetime("") is None


def test_non_string_int_returns_none():
    assert parse_datetime(42) is None  # type: ignore[arg-type]


def test_non_string_list_returns_none():
    assert parse_datetime([]) is None  # type: ignore[arg-type]


def test_garbage_string_returns_none():
    assert parse_datetime("not-a-date") is None


def test_partial_date_returns_none():
    assert parse_datetime("2026-07") is None


# --- Aware ISO strings ---


def test_utc_offset_aware():
    result = parse_datetime("2026-07-04T23:18:37+00:00")
    assert result is not None
    assert result.tzinfo is not None
    assert result.year == 2026
    assert result.month == 7
    assert result.day == 4


def test_z_suffix_aware():
    result = parse_datetime("2026-07-04T23:18:37Z")
    assert result is not None
    assert result.tzinfo is not None
    assert result == datetime(2026, 7, 4, 23, 18, 37, tzinfo=UTC)


def test_positive_offset():
    result = parse_datetime("2026-07-04T23:18:37+02:00")
    assert result is not None
    assert result.tzinfo is not None
    # Offset preserved
    assert result.utcoffset().total_seconds() == 7200  # type: ignore[union-attr]


def test_z_and_offset_equivalent():
    z = parse_datetime("2026-07-04T23:18:37Z")
    offset = parse_datetime("2026-07-04T23:18:37+00:00")
    assert z == offset


# --- Naive strings (assume_utc=True, the default) ---


def test_naive_iso_coerced_to_utc():
    result = parse_datetime("2026-07-04T23:18:37")
    assert result is not None
    assert result.tzinfo == UTC


def test_naive_iso_assume_utc_false_stays_naive():
    result = parse_datetime("2026-07-04T23:18:37", assume_utc=False)
    assert result is not None
    assert result.tzinfo is None


# --- datetime passthrough ---


def test_aware_datetime_passthrough():
    dt = datetime(2026, 7, 4, 23, 18, 37, tzinfo=UTC)
    result = parse_datetime(dt)
    assert result is dt


def test_naive_datetime_coerced_to_utc():
    dt = datetime(2026, 7, 4, 23, 18, 37)
    result = parse_datetime(dt)
    assert result is not None
    assert result.tzinfo == UTC
    assert result.year == 2026


def test_naive_datetime_assume_utc_false():
    dt = datetime(2026, 7, 4, 23, 18, 37)
    result = parse_datetime(dt, assume_utc=False)
    assert result is dt  # returned unchanged


def test_aware_datetime_non_utc_passthrough():
    from datetime import timedelta

    cest = timezone(timedelta(hours=2))
    dt = datetime(2026, 7, 4, 23, 18, 37, tzinfo=cest)
    result = parse_datetime(dt)
    assert result is dt
    assert result.tzinfo == cest


# --- Microseconds and date-only ---


def test_microseconds_preserved():
    result = parse_datetime("2026-07-04T23:18:37.123456+00:00")
    assert result is not None
    assert result.microsecond == 123456


def test_date_only_string_parsed_as_midnight_utc():
    # In Python 3.12, datetime.fromisoformat("2026-07-04") returns
    # datetime(2026, 7, 4, 0, 0) — naive midnight, coerced to UTC by default
    result = parse_datetime("2026-07-04")
    assert result is not None
    assert result == datetime(2026, 7, 4, 0, 0, tzinfo=UTC)


# --- Z-suffix edge cases ---


def test_z_suffix_with_microseconds():
    result = parse_datetime("2026-07-04T23:18:37.123456Z")
    assert result is not None
    assert result.tzinfo == UTC
    assert result.microsecond == 123456
