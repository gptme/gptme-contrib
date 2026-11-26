"""Fleet ``core@1`` / ``headline@1`` helpers for hand-built envelopes.

The legacy rollup ignores missing or unranked dimensions and defaults to healthy.
Page-derived builds keep their stricter all-blind-is-unknown rule in ``build``.
"""

from __future__ import annotations

import json
from pathlib import Path

from gptme_vitals.envelope import RANKABLE_STATUSES, Status, utcnow_rfc3339

CORE_SCHEMA = "fleet-vitals/core@1"
HEADLINE_SCHEMA = "fleet-vitals/headline@1"

# Worst-first; index = severity. Used by roll_up_status and the aggregator.
STATUS_ORDER = [str(status) for status in RANKABLE_STATUSES]
_VALID_STATUS = {str(status) for status in Status}


def roll_up_status(*statuses: str | None) -> str:
    """Worst-of rollup: down > degraded > stale > healthy.

    Unknown/None values are ignored (a missing dimension must not drag the
    rollup down — that's the staleness layer's job, not the agent's). Returns
    ``"healthy"`` if nothing usable was passed.
    """
    worst = None
    for s in statuses:
        if not s or s not in STATUS_ORDER:
            continue
        if worst is None or STATUS_ORDER.index(s) < STATUS_ORDER.index(worst):
            worst = s
    return worst or "healthy"


def make_core(
    agent: str,
    status: str | None = None,
    *,
    activity: dict | None = None,
    spend: dict | None = None,
    services: dict | None = None,
    generated_at: str | None = None,
    **extra: dict,
) -> dict:
    """Build a ``fleet-vitals/core@1`` dict.

    If ``status`` is omitted it's derived by rolling up the per-dimension
    ``status`` fields of the dimensions you pass. Pass it explicitly to
    override. ``extra`` lets an agent attach future dimensions without a
    helper change.
    """
    dims = {"activity": activity, "spend": spend, "services": services, **extra}
    present = {k: v for k, v in dims.items() if v is not None}
    if status is None:
        status = roll_up_status(*(d.get("status") for d in present.values()))
    if status not in _VALID_STATUS:
        raise ValueError(
            f"invalid status {status!r}; expected one of {sorted(_VALID_STATUS)}"
        )
    return {
        "schema": CORE_SCHEMA,
        "agent": agent,
        "generated_at": generated_at or utcnow_rfc3339(),
        "status": status,
        **present,
    }


def make_headline(
    agent: str,
    title: str,
    value: str,
    *,
    status: str = "healthy",
    trend: str | None = "flat",
    detail: str | None = None,
    link: str | None = None,
    generated_at: str | None = None,
) -> dict:
    """Build a ``fleet-vitals/headline@1`` dict.

    ``value`` is a pre-formatted display string — the agent owns presentation
    so the aggregator never needs to know what the metric means.
    """
    if status not in _VALID_STATUS:
        raise ValueError(
            f"invalid status {status!r}; expected one of {sorted(_VALID_STATUS)}"
        )
    headline = {
        "schema": HEADLINE_SCHEMA,
        "agent": agent,
        "title": title,
        "value": value,
        "status": status,
        "generated_at": generated_at or utcnow_rfc3339(),
    }
    if trend is not None:
        headline["trend"] = trend
    if detail is not None:
        headline["detail"] = detail
    if link is not None:
        headline["link"] = link
    return headline


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` atomically via a temp file in the same directory."""
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content)
    tmp.rename(path)


def emit(vitals_dir: str | Path, core: dict, headline: dict | None = None) -> None:
    """Write ``core.json`` (and optional ``headline.json``) into ``vitals_dir``.

    Creates the directory if needed. This is the one place that decides file
    names, so every agent's layout stays identical for the SSH-pull aggregator.
    Writes are atomic (write-to-temp + rename) so the aggregator never reads a
    partial file. A stale ``headline.json`` is removed when headline is None so
    ``core.json`` and ``headline.json`` stay in sync.
    """
    d = Path(vitals_dir)
    d.mkdir(parents=True, exist_ok=True)
    _atomic_write(d / "core.json", json.dumps(core, indent=2) + "\n")
    if headline is not None:
        _atomic_write(d / "headline.json", json.dumps(headline, indent=2) + "\n")
    else:
        stale = d / "headline.json"
        if stale.exists():
            stale.unlink()
