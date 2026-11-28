"""Private offline fleet card grid over native core exports or a fleet@1 export.

Only identity, timestamps, statuses, reachability and direct finite numeric core
metrics are projected. This module never collects data or reads/writes files.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from html import escape
from typing import Any

from gptme_vitals.envelope import Status
from gptme_vitals.fleet import CORE_SCHEMA
from gptme_vitals.theme import render_head_assets, render_theme_toggle

FLEET_SCHEMA = "fleet-vitals/fleet@1"
_RESERVED = {"schema", "agent", "generated_at", "status", "detail"}
_BADGES = {
    "healthy": "green",
    "degraded": "yellow",
    "stale": "yellow",
    "down": "red",
    "unknown": "unknown",
    "no-data": "unknown",
}


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _name(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value.strip()


def _status(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("status must use the vitals status vocabulary")
    try:
        return str(Status(value))
    except ValueError as exc:
        raise ValueError("status must use the vitals status vocabulary") from exc


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("generated_at must be a timezone-aware ISO timestamp")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.utcoffset() is None:
            raise ValueError("missing timezone")
        return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError(
            "generated_at must be a valid timezone-aware timestamp"
        ) from exc


def _finite(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} must be a finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError as exc:
        raise ValueError(f"{label} is out of range") from exc
    if not finite:
        raise ValueError(f"{label} must be finite")


def _badge(status: str) -> str:
    return f'<span class="badge badge-{_BADGES[status]}">{status}</span>'


def _dimensions(core: dict[str, Any]) -> str:
    rows = []
    for name, value in core.items():
        _name(name, "dimension name")
        if (
            name in _RESERVED
            or value is None
            or isinstance(value, str | int | float | bool)
        ):
            continue
        dimension = _object(value, f"{name} dimension")
        status = _status(dimension.get("status"))
        metrics = []
        for key, number in dimension.items():
            _name(key, "metric name")
            if key in {"status", "detail"}:
                continue
            if isinstance(number, int | float) and not isinstance(number, bool):
                _finite(number, key)
                metrics.append(f"<li>{escape(key)}: {number}</li>")
        rows.append(
            f'<section class="dimension"><h3>{escape(name)} {_badge(status)}</h3>'
            + (
                "<ul>" + "".join(metrics) + "</ul>"
                if metrics
                else "<p>No numeric metrics</p>"
            )
            + "</section>"
        )
    return "".join(rows) or "<p>No dimensions in this core export.</p>"


def _card(
    value: object,
    now: datetime,
    threshold: float,
    seen: set[str],
    wrapper: dict[str, Any] | None = None,
) -> str:
    core = _object(value, "core export") if value is not None else None
    # Native collectors use null/{} or an explicitly marked identity-only
    # placeholder for missing self reports. Accept only that exact contract;
    # any other partial/nonempty core must still pass full schema validation.
    if wrapper is not None and (
        core == {}
        or (
            wrapper.get("self_report_missing") is True
            and core is not None
            and set(core) == {"agent", "status", "generated_at"}
            and core["agent"] == wrapper.get("agent")
            and core["status"] == "no-data"
            and core["generated_at"] is None
        )
    ):
        core = None
    if core is None and wrapper is None:
        raise ValueError("core export must be an object")
    source = wrapper if wrapper is not None else core
    assert source is not None
    agent = _name(source.get("agent"), "agent")
    if agent in seen:
        raise ValueError(f"Duplicate agent: {agent}")
    seen.add(agent)

    reported = "unknown"
    freshness = "Unknown (no core export)"
    generated = "<p>No core export</p>"
    dimensions = ""
    effective = "unknown"
    if core is not None:
        if core.get("schema") != CORE_SCHEMA:
            raise ValueError(f"core schema must be {CORE_SCHEMA}")
        if _name(core.get("agent"), "agent") != agent:
            raise ValueError("wrapper agent must match core agent")
        stamp = _timestamp(core.get("generated_at"))
        reported = _status(core.get("status"))
        age = (now - stamp).total_seconds()
        if age < -60:
            freshness = "Future timestamp (clock skew)"
            effective = "down" if reported == "down" else "unknown"
        elif age > threshold:
            freshness = "Stale"
            effective = "down" if reported == "down" else "stale"
        else:
            freshness, effective = "Fresh", reported
        generated = f"<p>Generated: <time>{escape(core['generated_at'])}</time></p>"
        dimensions = _dimensions(core)

    observed = ""
    if wrapper is not None:
        reachable = wrapper.get("reachable")
        if not isinstance(reachable, bool):
            raise ValueError("reachable must be a boolean")
        # Null self-status is legitimate when a host has no usable core export.
        self_status = wrapper.get("self_status")
        if self_status is not None:
            self_status = _status(self_status)
        observed_status = _status(wrapper.get("observed_status"))
        exported_effective = _status(wrapper.get("effective_status"))
        observed = (
            f"<p>Reachable: {'yes' if reachable else 'no'}</p>"
            f"<p>Self status: {self_status or 'unknown'}</p>"
            f"<p>Observed status: {observed_status}</p>"
            f"<p>Exported effective status: {exported_effective}</p>"
        )
        signals = {observed_status, exported_effective, self_status, effective}
        # Observed outages remain visible even when a self-report is stale or
        # clock-skewed. Never let a healthy core mask an unreachable host.
        if not reachable or "down" in signals:
            effective = "down"
        elif freshness.startswith("Future"):
            effective = "unknown"
        elif freshness == "Stale":
            effective = "stale"
        else:
            effective = next(
                (
                    s
                    for s in ("degraded", "stale", "unknown", "no-data", "healthy")
                    if s in signals
                ),
                "unknown",
            )

    return (
        f'<article class="fleet-card" data-status="{effective}">'
        f"<h2>{escape(agent)}</h2><p>Effective status: {_badge(effective)}</p>"
        f"<p>Reported status: {reported}</p>{observed}"
        f"<p>Freshness: {freshness}</p>{generated}{dimensions}</article>"
    )


def render_fleet(
    cores: list[object] | dict[str, Any],
    *,
    now: datetime | None = None,
    stale_after_seconds: float = 900,
) -> str:
    """Render a private responsive grid, validating every input before returning.

    Accept individual native ``core@1`` dictionaries or a ``fleet@1`` envelope
    with ``agents`` rows wrapping cores. Missing cores are only allowed in fleet
    rows (for unreachable hosts); their freshness remains explicitly unknown.
    Freshness is recomputed from core timestamps, never trusted from age_seconds.
    Unknown status vocabulary, duplicate identities and malformed safe fields
    raise ValueError. Unlisted text, nested details and input URLs are not emitted.
    """
    _finite(stale_after_seconds, "stale_after_seconds")
    if stale_after_seconds < 0:
        raise ValueError("stale_after_seconds must be nonnegative")
    now = datetime.now(timezone.utc) if now is None else now
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    now = now.astimezone(timezone.utc)
    seen: set[str] = set()
    if isinstance(cores, dict):
        if cores.get("schema") != FLEET_SCHEMA:
            raise ValueError(f"fleet schema must be {FLEET_SCHEMA}")
        _timestamp(cores.get("generated_at"))
        if "fleet_status" in cores:
            _status(cores["fleet_status"])
        agents = cores.get("agents")
        if not isinstance(agents, list):
            raise ValueError("fleet agents must be an array")
        cards = []
        for value in agents:
            row = _object(value, "fleet agent")
            cards.append(_card(row.get("core"), now, stale_after_seconds, seen, row))
    elif isinstance(cores, list):
        cards = [_card(core, now, stale_after_seconds, seen) for core in cores]
    else:
        raise ValueError("fleet input must be a core array or fleet@1 object")
    body = "".join(cards) if cards else "<p>No agents in this export.</p>"
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Fleet vitals</title>{render_head_assets()}"
        "<style>main{max-width:1440px;margin:auto;padding:16px}"
        ".fleet-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr));gap:12px}"
        ".fleet-card{background:var(--tile-bg);border:1px solid var(--border);"
        "border-radius:8px;padding:12px;min-width:0;overflow-wrap:anywhere}"
        ".fleet-card h2{margin:0 0 8px}.fleet-card p{margin:4px 0}"
        ".dimension{border-top:1px solid var(--border);margin-top:8px;padding-top:4px}"
        ".dimension h3{font-size:1em;margin:4px 0}.dimension ul{margin:4px 0;padding-left:20px}"
        "</style></head><body><main>"
        f"{render_theme_toggle()}<h1>Fleet vitals</h1>"
        "<p>Private offline export view. Freshness is separate from reported health.</p>"
        f'<div class="fleet-grid">{body}</div></main></body></html>\n'
    )
