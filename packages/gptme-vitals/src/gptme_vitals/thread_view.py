"""Opt-in private views of ``gptme-sessions threads --json`` exports.

Consume the existing forest, never infer new links or read journals. Session
identities/titles stay in the private root; the normal public build is untouched.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from gptme_vitals.publish import write_private_html
from gptme_vitals.theme import render_head_assets, render_theme_toggle


class ThreadViewError(ValueError):
    """The input is not a usable threads export."""


@dataclass(frozen=True)
class _Session:
    id: str
    agent: str
    thread: str
    start: float
    end: float
    title: str
    predecessor: str | None
    kind: str | None
    key: str | None
    weight: float | None


def _text(row: dict[str, Any], key: str, *, optional: bool = False) -> str | None:
    value = row.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str) or (not optional and not value.strip()):
        raise ThreadViewError(f"session {key} must be a string")
    return value


def _number(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, float | int):
        raise ThreadViewError(f"session {key} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ThreadViewError(f"session {key} is out of range") from exc
    if not math.isfinite(number):
        raise ThreadViewError(f"session {key} must be finite")
    return number


def _stamp(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OverflowError, OSError) as exc:
        raise ThreadViewError("session timestamp is out of range") from exc


def _sessions(payload: dict[str, Any]) -> list[_Session]:
    if not isinstance(payload, dict) or not isinstance(payload.get("sessions"), list):
        raise ThreadViewError("expected a threads export with a sessions list")
    if "stats" in payload and not isinstance(payload["stats"], dict):
        raise ThreadViewError("threads stats must be an object")
    rows = []
    ids: set[str] = set()
    for raw in payload["sessions"]:
        if not isinstance(raw, dict):
            raise ThreadViewError("each session must be an object")
        sid, agent, thread = (_text(raw, key) for key in ("id", "agent", "thread"))
        assert sid is not None and agent is not None and thread is not None
        if sid in ids:
            raise ThreadViewError(f"duplicate session id: {sid}")
        ids.add(sid)
        start, end = _number(raw, "start"), _number(raw, "end")
        _stamp(start)
        _stamp(end)
        if end < start:
            raise ThreadViewError(f"session {sid}: end precedes start")
        rows.append(
            _Session(
                sid,
                agent,
                thread,
                start,
                end,
                _text(raw, "title", optional=True) or sid,
                _text(raw, "predecessor", optional=True),
                _text(raw, "kind", optional=True),
                _text(raw, "key", optional=True),
                _number(raw, "weight") if raw.get("weight") is not None else None,
            )
        )
    rows.sort(key=lambda row: (row.start, row.id))
    positions = {row.id: i for i, row in enumerate(rows)}
    for i, row in enumerate(rows):
        if row.predecessor in positions and positions[row.predecessor] >= i:
            raise ThreadViewError(f"session {row.id}: predecessor must be earlier")
    return rows


def join_thread_exports(exports: list[dict[str, Any]]) -> dict[str, Any]:
    """Join explicit IDs from native exports without mining new evidence.

    Identical overlapping rows collapse; conflicting identities refuse the join.
    No short-ID, title, or agent-name guessing is performed. Predecessors newly
    present in the union connect the existing forests, including across agents.
    """
    by_id: dict[str, _Session] = {}
    for payload in exports:
        for row in _sessions(payload):
            previous = by_id.get(row.id)
            if previous is not None and previous != row:
                raise ThreadViewError(f"conflicting session id: {row.id}")
            by_id[row.id] = row
    # Validate chronology over the union as well as over each source. An edge
    # outside one export can become a time-inverted edge in the joined set.
    rows = _sessions({"sessions": [asdict(row) for row in by_id.values()]})
    roots: dict[str, str] = {}
    joined = []
    for row in rows:
        root = (
            roots.get(row.predecessor, row.predecessor) if row.predecessor else row.id
        )
        roots[row.id] = root
        joined.append({**asdict(row), "thread": root})
    return {
        "sessions": joined,
        "stats": {
            "sessions": len(rows),
            "linked": sum(row.predecessor is not None for row in rows),
            "threads": len(set(roots.values())),
        },
    }


CSS = """
main { max-width: 1200px; margin: auto; padding: 16px; }
nav { display: flex; gap: 20px; margin: 12px 0; }
.muted { color: var(--dim); font-size: 12px; }
.scroll { overflow-x: auto; }
section { margin: 20px 0; padding: 12px; background: var(--card-bg);
  border: 1px solid var(--border); border-radius: 6px; }
h2 { font-size: 15px; overflow-wrap: anywhere; }
svg { display: block; min-width: 900px; width: 100%; }
svg text { fill: var(--text); font: 12px monospace; }
svg .evidence { fill: var(--dim); font-size: 10px; }
.edge { stroke: var(--dim); fill: none; opacity: .6; pointer-events: none; }
svg a { cursor: pointer; }
svg a:hover text { text-decoration: underline; }
.duration { stroke: var(--accent); stroke-width: 5; }
.node { fill: var(--accent); }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 8px; text-align: left; vertical-align: top;
  border-bottom: 1px solid var(--border); overflow-wrap: anywhere; }
th { font-size: 12px; color: var(--dim); }
tr:target { background: var(--tile-bg); outline: 1px solid var(--accent); }
code { font-size: 11px; }
""".strip()


def _document(title: str, body: str, rows: list[_Session]) -> str:
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>{render_head_assets()}<style>{CSS}</style>"
        "</head><body><main>"
        f"{render_theme_toggle()}<h1>{escape(title)}</h1>"
        '<nav aria-label="Thread views"><a href="index.html">Thread map</a>'
        '<a href="log.html">Thread log</a></nav>'
        f"<p>{len(rows)} sessions · {len({r.thread for r in rows})} threads</p>"
        '<p class="muted">Private export view. Links are best-predecessor evidence, '
        "not proof of causality. Missing links do not prove independent work. "
        "Times are UTC; journal excerpts are not included.</p>"
        f"{body}</main></body></html>\n"
    )


def _evidence(row: _Session, positions: dict[str, int]) -> str:
    if row.predecessor is None:
        return "No predecessor recorded"
    label = escape(row.predecessor)
    if row.predecessor in positions:
        label = f'<a href="#session-{positions[row.predecessor]}">{label}</a>'
    else:
        label += " (Outside export)"
    parts = [escape(v) for v in (row.kind, row.key) if v]
    if row.weight is not None:
        parts.append(f"weight {row.weight:g}")
    detail = f'<div class="muted">{" · ".join(parts)}</div>' if parts else ""
    return label + detail


def render_threads(payload: dict[str, Any]) -> tuple[str, str]:
    """Return map and chronological log HTML; ignore non-export fields.

    The input is never mutated. Dangling references remain visibly unresolved.
    All links use generated anchors, not identifiers or URLs supplied by input.
    """
    rows = _sessions(payload)
    positions = {row.id: i for i, row in enumerate(rows)}
    groups: dict[str, list[_Session]] = defaultdict(list)
    for row in rows:
        groups[row.thread].append(row)
    sections = []
    thread_positions = {thread: i for i, thread in enumerate(groups)}
    for thread, members in groups.items():
        first, last = min(r.start for r in members), max(r.end for r in members)
        span = max(last - first, 1)
        y = {row.id: 48 + i * 54 for i, row in enumerate(members)}

        def x(epoch: float) -> float:
            return 24 + 400 * (epoch - first) / span

        marks = []
        for row in members:
            if row.predecessor in y:
                parent = rows[positions[row.predecessor]]
                marks.append(
                    f'<path class="edge" d="M{x(parent.start):.1f},{y[parent.id]} '
                    f'L{x(row.start):.1f},{y[row.id]}"><title>'
                    f'{escape(row.kind or "link")}</title></path>'
                )
            evidence = "No predecessor recorded"
            if row.predecessor is not None:
                evidence = row.predecessor
                if row.predecessor not in positions:
                    evidence += " (Outside export)"
                evidence += " · " + " · ".join(v for v in (row.kind, row.key) if v)
                if row.weight is not None:
                    evidence += f" · weight {row.weight:g}"
            tooltip = escape(f"{row.agent}: {row.title} [{row.id}] · {evidence}")
            marks.append(
                f'<a href="log.html#session-{positions[row.id]}"><title>{tooltip}</title>'
                f'<rect x="0" y="{y[row.id] - 12}" width="1000" height="50" fill="transparent"/>'
                f'<line class="duration" x1="{x(row.start):.1f}" x2="{x(row.end):.1f}" '
                f'y1="{y[row.id]}" y2="{y[row.id]}"/>'
                f'<circle class="node" cx="{x(row.start):.1f}" cy="{y[row.id]}" r="4"/>'
                f'<text x="450" y="{y[row.id] + 4}">'
                f"{escape(row.agent)} · {escape(row.title[:65])}</text>"
                f'<text class="evidence" x="450" y="{y[row.id] + 20}">'
                f"{escape(evidence[:90])}</text></a>"
            )
        root = (
            rows[positions[thread]].title
            if thread in positions
            else thread + " (root outside export)"
        )
        sections.append(
            f'<section id="thread-{thread_positions[thread]}">'
            f"<h2>{escape(root)} · {len(members)} sessions</h2>"
            f'<p class="muted">{_stamp(first)} — {_stamp(last)} UTC</p>'
            f'<div class="scroll"><svg role="img" aria-label="Session predecessor map" '
            f'viewBox="0 0 1000 {max(84, 54 * len(members) + 36)}">'
            + "".join(marks)
            + "</svg></div></section>"
        )
    table_rows = []
    for i, row in enumerate(rows):
        table_rows.append(
            f'<tr id="session-{i}"><td>{_stamp(row.start)}<br>'
            f'<span class="muted">{row.end - row.start:g}s</span></td>'
            f"<td>{escape(row.agent)}</td><td>{escape(row.title)}<br>"
            f"<code>{escape(row.id)}</code><br>"
            f'<a href="index.html#thread-{thread_positions[row.thread]}">'
            f"Thread {escape(row.thread)}</a></td><td>{_evidence(row, positions)}</td></tr>"
        )
    empty = "<p>No sessions in this export.</p>"
    log = (
        (
            '<div class="scroll"><table><thead><tr><th>Started (UTC) / duration</th>'
            "<th>Agent</th><th>Session</th><th>Predecessor / evidence</th></tr></thead><tbody>"
            + "".join(table_rows)
            + "</tbody></table></div>"
        )
        if rows
        else empty
    )
    return (
        _document(
            "Thread map",
            (
                '<p class="muted">Bars show session duration; lines show predecessors. '
                "Each panel uses its own time scale.</p>" + "".join(sections)
            )
            if rows
            else empty,
            rows,
        ),
        _document("Thread log", log, rows),
    )


def write_threads(
    payload: dict[str, Any], *, private_root: Path, public_root: Path
) -> tuple[Path, Path]:
    """Write private HTML atomically per file, mode 0600. No public writes.

    Validate the complete export and both output paths before writing. Callers
    running concurrent exports must serialize the pair, like core/headline writes.
    """
    map_html, log_html = render_threads(payload)
    targets = write_private_html(
        {"threads/index.html": map_html, "threads/log.html": log_html},
        private_root=private_root,
        public_root=public_root,
    )
    return targets[0], targets[1]
