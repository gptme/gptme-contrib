"""Claude Code session log-tailer: zero-config ``app.agent.session`` blocks.

Claude Code writes one JSONL transcript per session under
``~/.claude/projects/<project-slug>/<session-id>.jsonl`` (``CLAUDE_CONFIG_DIR``
moves the root). Every user/assistant row carries ``timestamp``, ``sessionId``
and ``cwd``. We read only those fields — never prompts, replies or tool
payloads — and turn each session into working blocks: consecutive rows closer
than ``gap_s`` (default 15 min) belong to one block, so a session left open
overnight does not paint the whole night as agent time.

Each finished block becomes one event in the same ``app.agent.session`` bucket
the lifecycle hooks write to, so hook users and tailer users see the same lane.
This is the no-hook path: run it from a timer and Claude Code work appears on
the Timeline without editing any harness configuration.

A cursor per transcript records how many blocks were emitted. The trailing
block is held back until it is older than ``gap_s``, so it is emitted once,
complete, instead of growing under repeated runs.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import core
from .client import AWClient, Event

DEFAULT_GAP_S = 15 * 60
_ROW_TYPES = ("user", "assistant")


def default_projects_dir() -> Path:
    """Root of Claude Code transcripts (honors ``CLAUDE_CONFIG_DIR``)."""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return Path(base) / "projects"


def transcripts(projects_dir: Path | None = None) -> list[Path]:
    """All top-level session transcripts, oldest first (subagent logs excluded)."""
    root = projects_dir or default_projects_dir()
    if not root.exists():
        return []
    return sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime)


@dataclass
class WorkBlock:
    """One contiguous stretch of activity within a Claude Code session."""

    session_id: str
    workspace: str
    start: datetime
    end: datetime

    def to_event(self) -> Event:
        data = core.session_data(
            {
                "harness": "claude-code",
                "session_id": self.session_id,
                "workspace": self.workspace,
            }
        )
        return Event(
            timestamp=self.start.isoformat(),
            duration=(self.end - self.start).total_seconds(),
            data=data,
        )


def _parse_ts(ts: Any) -> datetime | None:
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_session(lines: Iterable[str], *, gap_s: float = DEFAULT_GAP_S) -> list[WorkBlock]:
    """Split one transcript into working blocks separated by gaps > ``gap_s``.

    Sidechain rows (subagent turns inlined in the parent log) are ignored; they
    overlap the parent's own rows and would only blur block boundaries.
    """
    rows: list[tuple[datetime, str, str]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("type") not in _ROW_TYPES:
            continue
        if rec.get("isSidechain"):
            continue
        ts = _parse_ts(rec.get("timestamp"))
        sid = rec.get("sessionId")
        if ts is None or not isinstance(sid, str) or not sid:
            continue
        cwd = rec.get("cwd")
        rows.append((ts, sid, cwd if isinstance(cwd, str) else ""))

    rows.sort(key=lambda r: r[0])
    blocks: list[WorkBlock] = []
    for ts, sid, cwd in rows:
        last = blocks[-1] if blocks else None
        if last and last.session_id == sid and (ts - last.end).total_seconds() <= gap_s:
            last.end = ts
            last.workspace = cwd or last.workspace
        else:
            blocks.append(WorkBlock(session_id=sid, workspace=cwd, start=ts, end=ts))
    return blocks


def _cursor_path(transcript: Path) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", str(transcript.absolute()))
    return core.state_dir() / f"cc-cursor-{safe}.json"


def _read_emitted(transcript: Path) -> int:
    try:
        data = json.loads(_cursor_path(transcript).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    count = data.get("emitted_blocks") if isinstance(data, dict) else None
    return count if isinstance(count, int) and count >= 0 else 0


def _write_emitted(transcript: Path, count: int) -> None:
    path = _cursor_path(transcript)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"emitted_blocks": count}), encoding="utf-8")


def emit_file(
    client: AWClient,
    hostname: str,
    path: Path,
    *,
    gap_s: float = DEFAULT_GAP_S,
    now: datetime | None = None,
) -> int:
    """Emit the finished, not-yet-emitted blocks of one transcript.

    Returns the number of events posted. A block is finished once its last row
    is more than ``gap_s`` old; the in-progress trailing block waits. If another
    invocation owns this transcript, skip it until the next run.
    """
    lock_path = _cursor_path(path).with_suffix(".lock.sqlite3")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # SQLite provides a cross-platform process lock, released even on a crash.
    # Keep this separate from the JSON cursor and never unlink the lock file.
    lock = sqlite3.connect(lock_path, timeout=0)
    try:
        try:
            lock.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if str(exc) == "database is locked":
                return 0
            raise
        return _emit_file_locked(client, hostname, path, gap_s=gap_s, now=now)
    finally:
        lock.close()


def _emit_file_locked(
    client: AWClient,
    hostname: str,
    path: Path,
    *,
    gap_s: float,
    now: datetime | None,
) -> int:
    now = now or datetime.now(timezone.utc)
    text = path.read_text(encoding="utf-8", errors="replace")
    blocks = parse_session(text.splitlines(), gap_s=gap_s)
    finished = [b for b in blocks if (now - b.end).total_seconds() > gap_s]
    emitted = _read_emitted(path)
    new = finished[emitted:]
    if not new:
        return 0

    bucket = core.bucket_id(hostname)
    client.ensure_bucket(bucket, core.BUCKET_TYPE, core.CLIENT_NAME, hostname)
    for block in new:
        client.post_event(bucket, block.to_event())
        emitted += 1
        _write_emitted(path, emitted)
    return len(new)
