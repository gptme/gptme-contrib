"""Private standard pages over native CLI exports; no collection or inference."""

from __future__ import annotations

import math
from html import escape
from typing import Any

from gptme_vitals.envelope import Status
from gptme_vitals.fleet import CORE_SCHEMA
from gptme_vitals.theme import render_head_assets, render_theme_toggle


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _text(row: dict[str, Any], key: str, *, required: bool = False) -> str:
    value = row.get(key)
    if value is None and not required:
        return "Unknown"
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(
            f"{key} must be {'a nonempty' if required else 'a nullable'} string"
        )
    return escape(value)


def _number(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if value is None:
        return "Unknown"
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{key} must be a nullable finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError as exc:
        raise ValueError(f"{key} is out of range") from exc
    if not finite:
        raise ValueError(f"{key} must be finite")
    return str(value)


def _table(headers: tuple[str, ...], rows: list[list[str]], empty: str) -> str:
    if not rows:
        return f"<p>{empty}</p>"
    return (
        '<div class="scroll"><table><thead><tr>'
        + "".join(f"<th>{heading}</th>" for heading in headers)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
            for row in rows
        )
        + "</tbody></table></div>"
    )


def _sessions(payload: object) -> str:
    if not isinstance(payload, list):
        raise ValueError("sessions must be the array from gptme-sessions query --json")
    rows = []
    for value in payload:
        row = _object(value, "session")
        sid = _text(row, "session_id", required=True)
        rows.append(
            [
                _text(row, "start_time"),
                sid + "<br>" + _text(row, "session_name"),
                _text(row, "model") + "<br>" + _text(row, "harness"),
                _text(row, "outcome"),
                _number(row, "duration_seconds"),
                _number(row, "trajectory_grade"),
            ]
        )
    return (
        "<p>Recorded session start, duration in seconds and trajectory grade. "
        "Unknown means unrecorded; record creation time is not used as session start.</p>"
        + _table(
            (
                "Started",
                "Session ID / name",
                "Model / harness",
                "Outcome",
                "Duration (s)",
                "Grade",
            ),
            rows,
            "No sessions in this export.",
        )
    )


def _tasks(payload: object) -> str:
    source = _object(payload, "tasks export")
    tasks = source.get("tasks")
    count = source.get("count")
    if not isinstance(tasks, list):
        raise ValueError("tasks export must contain a tasks array")
    if "count" in source and (
        isinstance(count, bool) or not isinstance(count, int) or count != len(tasks)
    ):
        raise ValueError("tasks count must equal the number of exported tasks")
    rows = []
    for value in tasks:
        row = _object(value, "task")
        rows.append(
            [
                _text(row, "id", required=True) + "<br>" + _text(row, "name"),
                *(
                    _text(row, key)
                    for key in (
                        "state",
                        "priority",
                        "assigned_to",
                        "pool",
                        "next_action",
                        "waiting_for",
                    )
                ),
            ]
        )
    return (
        "<p>Task IDs and names are filename slugs. State does not establish readiness; "
        "claims and dependency checks remain with gptodo.</p>"
        + _table(
            (
                "ID / name",
                "State",
                "Priority",
                "Assigned to",
                "Pool",
                "Next action",
                "Waiting for",
            ),
            rows,
            "No tasks in this export.",
        )
    )


def _status(row: dict[str, Any]) -> str:
    status = row.get("status")
    if status is None:
        return "Unknown"
    try:
        return escape(str(Status(status)))
    except (TypeError, ValueError) as exc:
        raise ValueError("status must use the vitals status vocabulary") from exc


def _vitals(payload: object) -> str:
    core = _object(payload, "vitals core")
    if core.get("schema") != CORE_SCHEMA:
        raise ValueError(f"vitals schema must be {CORE_SCHEMA}")
    agent = _text(core, "agent", required=True)
    generated = _text(core, "generated_at")
    status = _status(core)
    rows = []
    for name, dimension in core.items():
        if (
            name in {"schema", "agent", "status", "generated_at", "detail"}
            or dimension is None
        ):
            continue
        if isinstance(dimension, str | int | float | bool):
            continue  # Scalar metadata is not a dimension.
        if not isinstance(name, str):
            raise ValueError("dimension names must be strings")
        dimension = _object(dimension, f"{name} dimension")
        metrics = []
        for key, value in dimension.items():
            if key == "status" or not isinstance(key, str):
                continue
            if isinstance(value, int | float) and not isinstance(value, bool):
                metrics.append(f"{escape(key)}: {_number(dimension, key)}")
        rows.append(
            [
                escape(name),
                _status(dimension),
                "<br>".join(metrics) or "No numeric metrics",
            ]
        )
    return (
        f"<p>Agent: {agent} · Status: {status} · Generated: {generated}</p>"
        + _table(
            ("Dimension", "Status", "Metrics"),
            rows,
            "No dimensions in this core export.",
        )
    )


def render_standard_page(kind: str, payload: object) -> str:
    """Return a private page over one native export; never read or write files."""
    renderers = {"sessions": _sessions, "tasks": _tasks, "vitals": _vitals}
    if not isinstance(kind, str) or kind not in renderers:
        raise ValueError("page kind must be sessions, tasks or vitals")
    body = renderers[kind](payload)
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{kind.title()}</title>{render_head_assets()}"
        "<style>main{max-width:1200px;margin:auto;padding:16px}.scroll{overflow-x:auto}"
        "table{width:100%;border-collapse:collapse}th,td{text-align:left;vertical-align:top;"
        "padding:8px;border-bottom:1px solid var(--border);overflow-wrap:anywhere}</style>"
        "</head><body><main>"
        f"{render_theme_toggle()}<h1>{kind.title()}</h1><p>Private export view.</p>"
        f"{body}</main></body></html>\n"
    )
