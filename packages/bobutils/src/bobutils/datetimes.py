"""Canonical datetime parsing utilities."""

from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc


def parse_datetime(
    value: str | datetime | None, *, assume_utc: bool = True
) -> datetime | None:
    """Parse an ISO 8601 timestamp string into an aware datetime.

    Handles:
    - None / non-string input → returns None
    - Already a datetime → coerces naive to UTC if assume_utc, returns as-is if aware
    - Z-suffix (both Python 3.11+ native and manual replacement)
    - Naive result → coerces to UTC when assume_utc=True
    - Unparseable string → returns None (never raises)

    Callers needing raise semantics should check for None and raise locally.

    Note: the metaproductivity wait-value normalizer (parse_wait_value) is a
    separate, bespoke function that handles Nd/Nh/cron strings in addition to
    ISO timestamps — it is NOT replaced by this helper.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC) if assume_utc else value
        return value

    if not isinstance(value, str):
        return None

    # Python 3.11+ handles Z natively, but normalize proactively for clarity
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC) if assume_utc else parsed
    return parsed
