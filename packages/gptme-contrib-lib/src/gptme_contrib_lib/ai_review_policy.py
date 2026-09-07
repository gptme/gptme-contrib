"""Shared policy for deciding which AI-review findings block a merge."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TypedDict

FindingRow = Mapping[str, object]


class Shortfall(TypedDict):
    """One finding that prevents the AI-review gate from accepting the head."""

    fp: str
    severity: str | None
    reason: str


def blocking_shortfall(rows: Sequence[FindingRow]) -> list[Shortfall]:
    """Return every finding that remains blocking under the shared policy.

    Producers normalize transport-specific GitHub payloads into rows. This
    function owns the policy: readable P2+ findings never block; unreadable
    severity fails closed; and P0/P1 findings block until explicitly disposed,
    superseded by the latest review, or both replied to and resolved.
    """
    shortfalls: list[Shortfall] = []
    for row in rows:
        severity_value = row.get("severity")
        severity = str(severity_value) if severity_value is not None else None
        if severity is not None and severity not in {"P0", "P1"}:
            continue
        if row.get("disposition") or row.get("superseded"):
            continue
        if row.get("isResolved") and bool(row.get("replied")):
            continue
        label = (
            f"{severity} finding" if severity else "finding with unreadable severity"
        )
        reason = (
            f"{label} resolved without a reply (not disposed)"
            if row.get("isResolved")
            else f"{label} is not resolved"
        )
        shortfalls.append(
            {"fp": str(row.get("fp") or ""), "severity": severity, "reason": reason}
        )
    return shortfalls
