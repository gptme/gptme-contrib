"""Canonical duration-string parsing.

Replaces ~10 per-file ``parse_duration`` implementations that had drifted into
inconsistent unit sets ("smhd" vs "dh" vs "d"-only), unanchored regexes that
accepted trailing garbage ("7dx"), and mixed error behavior. One grammar:

    <non-negative integer><unit>   with unit in s/m/h/d/w (case-insensitive)

Bare integers are rejected unless the caller opts in via ``default_unit``
(preserves CLI compatibility for callers that historically accepted e.g. "30"
meaning 30 days). Zero is accepted; callers that require a positive duration
enforce that themselves.
"""

from __future__ import annotations

import re
from datetime import timedelta

__all__ = ["parse_duration"]

_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}

_DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)


def parse_duration(value: str, *, default_unit: str | None = None) -> timedelta:
    """Parse a duration string like "90s", "45m", "24h", "7d", "2w".

    Args:
        value: The duration string. Surrounding whitespace is ignored.
        default_unit: If set (one of "s"/"m"/"h"/"d"/"w"), a bare integer is
            interpreted in that unit ("30" with default_unit="d" → 30 days).
            If None (default), bare integers raise ValueError.

    Raises:
        ValueError: On empty input, unknown unit, trailing garbage, negative
            values, or a bare integer without ``default_unit``.
    """
    if default_unit is not None and default_unit not in _UNIT_SECONDS:
        raise ValueError(
            f"default_unit must be one of {sorted(_UNIT_SECONDS)}, got {default_unit!r}"
        )
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ValueError(f"empty duration string: {value!r}")
    match = _DURATION_RE.fullmatch(text)
    if match is None:
        if default_unit is not None and text.isdigit():
            return timedelta(seconds=int(text) * _UNIT_SECONDS[default_unit])
        raise ValueError(
            f"invalid duration {value!r}: expected <integer><unit> with unit in s/m/h/d/w"
        )
    amount, unit = int(match.group(1)), match.group(2).lower()
    return timedelta(seconds=amount * _UNIT_SECONDS[unit])
