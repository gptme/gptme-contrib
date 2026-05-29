"""Session bucket conventions and crash-robust state for aw-watcher-agent.

A session is modelled as a single event in the ``app.agent.session`` bucket.
``emit-start`` posts a zero-duration start event and records its server id; on
``emit-end`` we delete that placeholder and post one clean event carrying the
full duration plus ``outcome``. This yields exactly one Timeline block per
session with complete metadata.

Why not heartbeat-extend the start event? ActivityWatch only merges consecutive
events with *identical* ``data``. ``outcome`` is unknown until the session ends
and changes the payload, so a heartbeat with the outcome would not merge with
the start event. Delete-then-repost keeps a single block while still recording
the outcome. If ``emit-end`` never runs (crash), the zero-duration start event
still marks that the session began.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

BUCKET_TYPE = "app.agent.session"
CLIENT_NAME = "aw-watcher-agent"

# Per-tool activity events live in their own bucket type. Phase 2 originally
# proposed reusing the session bucket to keep "AI work" as a single Timeline
# lane, but session events (one long block spanning the whole run) and per-tool
# events (many short blocks) overlap in time, which ActivityWatch cannot render
# as a single clean lane. A sibling bucket keeps both lanes honest; aw-webui can
# still stack them. See the Phase 2 PR for the design note.
ACTIVITY_BUCKET_TYPE = "app.agent.activity"

# Stable session fields (everything except outcome, which is end-only).
START_FIELDS = ("harness", "model", "category", "session_id", "trigger", "workspace")
ACTIVITY_FIELDS = (
    "harness",
    "session_id",
    "tool",
    "status",
    "model",
    "category",
    "trigger",
    "workspace",
)


def bucket_id(hostname: str) -> str:
    """Bucket id following the ``aw-watcher-<name>_<hostname>`` convention."""
    return f"{CLIENT_NAME}_{hostname}"


def activity_bucket_id(hostname: str) -> str:
    """Bucket id for per-tool activity events (sibling of the session bucket)."""
    return f"{CLIENT_NAME}-activity_{hostname}"


def state_dir() -> Path:
    """Per-session state directory (honors XDG_STATE_HOME)."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / CLIENT_NAME


def state_path(session_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in session_id)
    return state_dir() / f"session-{safe}.json"


def write_state(session_id: str, payload: dict[str, Any]) -> Path:
    path = state_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def read_state(session_id: str) -> dict[str, Any] | None:
    path = state_path(session_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def clear_state(session_id: str) -> None:
    path = state_path(session_id)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def session_data(args: dict[str, Any], *, outcome: str | None = None) -> dict[str, str]:
    """Build the event ``data`` map from CLI args, dropping empty values."""
    data = {f: str(args[f]) for f in START_FIELDS if args.get(f)}
    if outcome:
        data["outcome"] = str(outcome)
    return data


def activity_data(args: dict[str, Any]) -> dict[str, str]:
    """Build per-tool activity event data, dropping empty values.

    Raises ValueError if 'tool' is absent or empty — it is the semantic
    discriminator for per-tool heartbeats and cannot be safely omitted.
    """
    if not args.get("tool"):
        raise ValueError("activity_data: 'tool' is required but missing or empty")
    return {f: str(args[f]) for f in ACTIVITY_FIELDS if args.get(f)}
