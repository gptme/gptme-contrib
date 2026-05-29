"""Codex transcript log-tailer: emit per-tool ``app.agent.activity`` heartbeats.

Phase 2 fallback for harnesses that cannot host an in-process gptme plugin hook.
Codex writes rollout transcripts to ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``.
Each line is ``{"timestamp", "type", "payload"}``. A tool call is a
``response_item`` whose ``payload.type == "function_call"`` (carrying ``name``,
``call_id``, ``arguments``); its result is a later ``function_call_output`` with
the matching ``call_id``. We pair them, compute a duration, derive a coarse
status from the output, and emit one activity event per call into the
``aw-watcher-agent-activity_<hostname>`` bucket.

The parser (:func:`parse_rollout`) is pure and stdlib-only so it is testable
without a live aw-server or a real Codex session. Emission (:func:`emit_file`)
reuses the vendored :class:`~aw_watcher_agent.client.AWClient` and heartbeats so
adjacent calls of the same tool merge into a single Timeline block.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .client import AWClient, Event
from . import core

# Codex marks shell exits as "Process exited with code N" / "exited with code N".
_EXIT_CODE_RE = re.compile(r"exited with code (\d+)")

# Default duration (s) for a call whose output we never saw (crash / truncation).
_UNPAIRED_DURATION = 0.0


def default_sessions_dir() -> Path:
    """Root of Codex rollout transcripts (honors ``CODEX_HOME``)."""
    base = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return Path(base) / "sessions"


def latest_rollout(sessions_dir: Path | None = None) -> Path | None:
    """Most recently modified ``rollout-*.jsonl`` under ``sessions_dir``."""
    root = sessions_dir or default_sessions_dir()
    files = sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


@dataclass
class ToolActivity:
    """One paired Codex tool call: name, coarse status, and wall-clock duration."""

    tool: str
    status: str
    duration_ms: int
    timestamp: str  # ISO 8601 start time (the function_call timestamp)
    call_id: str
    session_id: str

    def to_event(self, *, min_duration_s: float = 0.0) -> Event:
        """Build an AW event. ``data`` excludes duration so same-tool/status
        calls merge under heartbeat; the per-call duration drives the block."""
        data = {"tool": self.tool, "status": self.status}
        if self.session_id:
            data["session_id"] = self.session_id
        return Event(
            timestamp=self.timestamp,
            duration=max(self.duration_ms / 1000.0, min_duration_s),
            data=data,
        )


def _status_from_output(output: Any) -> str:
    """Coarse success/error/completed status from a function_call_output."""
    if not isinstance(output, str) or not output.strip():
        return "completed"
    match = _EXIT_CODE_RE.search(output)
    if match:
        return "success" if match.group(1) == "0" else "error"
    return "completed"


def _parse_ts(ts: str) -> datetime | None:
    try:
        # Codex stamps RFC3339 with a trailing 'Z'; fromisoformat wants +00:00.
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _iter_records(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def parse_rollout(lines: Iterable[str]) -> tuple[str, list[ToolActivity]]:
    """Parse Codex rollout JSONL into ``(session_id, [ToolActivity, ...])``.

    Pairs each ``function_call`` with its matching ``function_call_output`` by
    ``call_id``. A call with no output (crash/truncation) is still emitted with
    a zero duration and ``completed`` status so the Timeline marks that it ran.
    """
    session_id = ""
    # Preserve call order; map call_id -> pending call record.
    pending: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for rec in _iter_records(lines):
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type")

        if rec.get("type") == "session_meta" and not session_id:
            sid = payload.get("id")
            if isinstance(sid, str):
                session_id = sid
            continue

        if ptype == "function_call":
            call_id = payload.get("call_id")
            if not isinstance(call_id, str):
                continue
            pending[call_id] = {
                "tool": str(payload.get("name") or "unknown"),
                "start": rec.get("timestamp"),
            }
            order.append(call_id)
        elif ptype == "function_call_output":
            call_id = payload.get("call_id")
            call = pending.get(call_id) if isinstance(call_id, str) else None
            if call is not None:
                call["end"] = rec.get("timestamp")
                call["status"] = _status_from_output(payload.get("output"))

    activities: list[ToolActivity] = []
    for call_id in order:
        call = pending[call_id]
        start = call.get("start")
        if not isinstance(start, str):
            continue
        duration_ms = 0
        end = call.get("end")
        if isinstance(end, str):
            t0, t1 = _parse_ts(start), _parse_ts(end)
            if t0 and t1:
                duration_ms = max(int((t1 - t0).total_seconds() * 1000), 0)
        activities.append(
            ToolActivity(
                tool=call["tool"],
                status=call.get("status", "completed"),
                duration_ms=duration_ms,
                timestamp=start,
                call_id=call_id,
                session_id=session_id,
            )
        )
    return session_id, activities


def emit_file(
    client: AWClient,
    hostname: str,
    path: Path,
    *,
    pulsetime: float = 5.0,
) -> int:
    """Parse one rollout file and emit its tool activities. Returns the count.

    ``pulsetime`` controls heartbeat merging: consecutive same-tool/same-status
    calls within this many seconds collapse into a single Timeline block.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    _, activities = parse_rollout(text.splitlines())
    if not activities:
        return 0
    bucket = core.activity_bucket_id(hostname)
    client.ensure_bucket(bucket, core.ACTIVITY_BUCKET_TYPE, core.CLIENT_NAME, hostname)
    for activity in activities:
        client.heartbeat(bucket, activity.to_event(), pulsetime=pulsetime)
    return len(activities)
