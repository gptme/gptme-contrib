"""Canonical coerce_int helper.

Replaces 5 per-script ``_coerce_int`` definitions whose core logic was
identical (None/bool → default, int → int, float → int(round), str → int)
but varied in whether failure returns 0 or None.

Callers that intentionally convert bool to int (e.g. True→1) keep a local
function — see validate_no_duplicate_utils.py ALLOWLIST.
"""

from __future__ import annotations

from typing import overload

__all__ = ["coerce_int", "coerce_int_nullable"]


@overload
def coerce_int(value: object) -> int: ...
@overload
def coerce_int(value: object, default: int) -> int: ...
@overload
def coerce_int(value: object, default: None) -> int | None: ...


def coerce_int(value: object, default: int | None = 0) -> int | None:
    """Coerce a mixed value to int.

    Guards against bool (``isinstance(True, int)`` is True in Python),
    rounds floats, strips whitespace from strings. Returns *default* for
    None, bool, or unparseable values.

    Args:
        value: Value to coerce.
        default: Returned when value cannot be coerced. Use ``None`` for
            nullable callers; defaults to 0 (non-nullable).

    Returns:
        Coerced int, or *default*.
    """
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError:
            pass
    return default


def coerce_int_nullable(value: object) -> int | None:
    """Coerce a mixed value to int, returning None for uncoercible values.

    Convenience wrapper around :func:`coerce_int` for callers that previously
    defined ``_coerce_int(value) -> int | None``.
    """
    return coerce_int(value, default=None)
