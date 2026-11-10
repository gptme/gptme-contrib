"""Coercion and percent formatting shared by dashboard renderers.

Only the agent-agnostic helpers live here. Bob's harm-recency, sweep-label and
subscription-colour formatters encode Bob's own pages and stay in his repo.
"""

from __future__ import annotations


def as_float(value: object) -> float | None:
    """Return ``value`` as a float, or ``None`` for bools and non-numbers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def as_int(value: object) -> int | None:
    """Return an int (finite floats truncate), or ``None`` otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return int(value)
        except (ValueError, OverflowError):
            return None
    return None


def format_pct(value: object) -> str:
    """Format a 0..1 fraction as ``NN%``; ``?`` when it is not a number."""
    numeric = as_float(value)
    return "?" if numeric is None else f"{numeric:.0%}"
