"""The event schema: one normalized record per observable agent step.

Every harness extractor emits :class:`AgentEvent` objects, and every sink
(sound player, publisher, dashboard) consumes them. The schema is deliberately
small and content-free: tool *names* and *classes*, never prompts, replies,
commands or tool output.

``detail`` holds extras: ``path`` for file writes; ``repo`` (GitHub
``owner/name``), ``sha`` and ``url`` for commits, pushes and PRs. The stream is
private (stdout, ssh). A sink that publishes beyond the agent's own host must
drop ``path`` and only forward ``repo``/``sha``/``url`` for public repos.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

#: Event kinds, in rough order of how often they occur.
KINDS = (
    "tool",  # a tool call started (see tool_class)
    "turn_end",  # the agent finished a turn and is waiting (the "ding")
    "error",  # a tool call failed
    "commit",  # a git commit succeeded
    "push",  # a git push succeeded
    "pr_open",  # a pull request was created
    "session_start",  # a new session (trajectory) appeared
)

#: Harness-agnostic tool classes, so sinks need not know tool names per harness.
TOOL_CLASSES = ("read", "search", "write", "shell", "test", "web", "agent", "other")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def normalize_ts(ts: Any) -> str:
    """Any harness timestamp as UTC ISO 8601 with milliseconds and ``+00:00``.

    Claude Code and Codex write ``...Z``; gptme writes naive local time, which
    is read as the local time of the host running the follower (the agent's own
    host). One format means events from different harnesses order correctly and
    compare as strings. Unparseable or missing values fall back to now.
    """
    if not isinstance(ts, str) or not ts.strip():
        return utc_now()
    text = ts.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return utc_now()
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive: local time
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class AgentEvent:
    kind: str
    ts: str
    harness: str
    session: str
    agent: str = ""
    tool: str | None = None
    tool_class: str | None = None
    subagent: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "kind": self.kind,
            "ts": self.ts,
            "agent": self.agent,
            "harness": self.harness,
            "session": self.session,
        }
        if self.tool is not None:
            d["tool"] = self.tool
        if self.tool_class is not None:
            d["tool_class"] = self.tool_class
        if self.subagent:
            d["subagent"] = True
        if self.detail:
            d["detail"] = self.detail
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AgentEvent:
        """Parse an event, ignoring unknown keys (newer producers may add fields)."""
        detail = d.get("detail")
        return cls(
            kind=str(d.get("kind", "")),
            ts=str(d.get("ts", "")),
            harness=str(d.get("harness", "")),
            session=str(d.get("session", "")),
            agent=str(d.get("agent", "")),
            tool=d.get("tool") if isinstance(d.get("tool"), str) else None,
            tool_class=d.get("tool_class") if isinstance(d.get("tool_class"), str) else None,
            subagent=bool(d.get("subagent", False)),
            detail=detail if isinstance(detail, dict) else {},
        )
