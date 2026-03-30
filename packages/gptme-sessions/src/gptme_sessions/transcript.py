"""Normalized session transcript for external consumers.

Reads raw harness-specific JSONL files and produces a stable, version-tagged
JSON contract that external consumers (dashboards, fleet operators, analysis
tools) can depend on without parsing harness-specific formats directly.

Supported harnesses: gptme, claude-code, codex, copilot.

Schema version: 1 (increment when breaking changes are made to the output shape).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .discovery import (
    extract_project,
    extract_session_name,
)
from .signals import (
    _parse_timestamp,  # type: ignore[attr-defined]  # private but co-located
    detect_format,
    parse_trajectory,
)

TRANSCRIPT_SCHEMA_VERSION = 1

Role = Literal["user", "assistant", "system", "tool_result"]


@dataclass
class NormalizedMessage:
    """A single turn in a normalized session transcript.

    Fields
    ------
    role : str
        One of ``"user"``, ``"assistant"``, ``"system"``, ``"tool_result"``.
    content : str
        Human-readable text content of the message. For tool calls this is the
        text portion of the assistant turn. For tool results it is the raw
        result string.
    timestamp : str | None
        ISO 8601 timestamp if available in the source record.
    tool_name : str | None
        Name of the tool called (assistant turns with tool use only).
    tool_input : dict | None
        Structured input arguments to the tool call, if available.
    tool_result : str | None
        Text content of the tool result (tool_result role only).
    is_error : bool
        True when this message represents a tool error / failed tool result.
    """

    role: str
    content: str
    timestamp: str | None = None
    tool_name: str | None = None
    tool_input: dict | None = None
    tool_result: str | None = None
    is_error: bool = False

    def to_dict(self) -> dict:
        """Serialize to JSON-compatible dict (omits ``None`` and ``False`` values)."""
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None and v is not False}


@dataclass
class SessionTranscript:
    """Normalized, harness-agnostic session detail and transcript.

    This is the stable machine-readable contract for external consumers.
    The ``schema_version`` field lets consumers detect breaking changes.

    Fields
    ------
    schema_version : int
        Schema version. Currently ``1``.
    session_id : str
        Opaque session identifier (harness-specific, e.g. UUID or path stem).
    harness : str
        One of ``"gptme"``, ``"claude-code"``, ``"codex"``, ``"copilot"``.
    session_name : str | None
        Human-readable session name (e.g. ``"dancing-blue-fish"``).
    project : str | None
        Workspace / project path detected from session metadata.
    model : str | None
        Raw model string from the session (e.g. ``"claude-opus-4-6"``).
    started_at : str | None
        ISO 8601 timestamp of the first message.
    last_activity : str | None
        ISO 8601 timestamp of the last message.
    trajectory_path : str
        Absolute path to the source JSONL file.
    capabilities : list[str]
        Capabilities available for this session. Phase 1 always contains
        ``"view_transcript"`` when messages were successfully read.
    messages : list[NormalizedMessage]
        Normalized transcript messages in chronological order.
    """

    schema_version: int
    session_id: str
    harness: str
    trajectory_path: str
    capabilities: list[str] = field(default_factory=list)
    messages: list[NormalizedMessage] = field(default_factory=list)
    session_name: str | None = None
    project: str | None = None
    model: str | None = None
    started_at: str | None = None
    last_activity: str | None = None

    def to_dict(self) -> dict:
        """Serialize to JSON-compatible dict."""
        d = asdict(self)
        d["messages"] = [m.to_dict() for m in self.messages]
        return d

    def to_json(self, indent: int | None = 2) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), indent=indent, default=str)


# ---------------------------------------------------------------------------
# Format-specific normalizers
# ---------------------------------------------------------------------------


def _ts_str(ts: datetime | None) -> str | None:
    """Convert a datetime to ISO 8601 string, or None."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


def _normalize_gptme(msgs: list[dict]) -> list[NormalizedMessage]:
    """Normalize gptme conversation.jsonl messages.

    gptme format: each record has top-level ``role``, ``content``, ``timestamp``.
    Role values: ``"user"``, ``"assistant"``, ``"system"``.
    """
    normalized: list[NormalizedMessage] = []
    for msg in msgs:
        role = msg.get("role", "")
        if role not in ("user", "assistant", "system"):
            continue
        content = msg.get("content", "") or ""
        ts = _ts_str(_parse_timestamp(msg.get("timestamp", "")))
        normalized.append(
            NormalizedMessage(
                role=role,
                content=content if isinstance(content, str) else json.dumps(content),
                timestamp=ts,
            )
        )
    return normalized


def _normalize_cc(msgs: list[dict]) -> list[NormalizedMessage]:
    """Normalize Claude Code .jsonl messages.

    CC format: each record has ``type`` in (``"user"``, ``"assistant"``, ``"result"``).
    - assistant: message.content is a list of blocks (text or tool_use)
    - user: message.content is a list containing tool_result blocks
    """
    normalized: list[NormalizedMessage] = []

    for record in msgs:
        rec_type = record.get("type", "")
        ts = _ts_str(_parse_timestamp(record.get("timestamp", "")))

        if rec_type == "assistant":
            content_list = record.get("message", {}).get("content", [])
            if not isinstance(content_list, list):
                continue
            # Combine assistant text into one turn, then emit tool calls after it.
            text_parts: list[str] = []
            tool_messages: list[NormalizedMessage] = []
            for item in content_list:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
                elif item.get("type") == "tool_use":
                    tool_name = item.get("name", "")
                    tool_input = item.get("input") or {}
                    tool_messages.append(
                        NormalizedMessage(
                            role="assistant",
                            content="",
                            timestamp=ts,
                            tool_name=tool_name,
                            tool_input=tool_input if isinstance(tool_input, dict) else {},
                        )
                    )
            # Emit the text turn before tool calls (if any text)
            text = "\n".join(p for p in text_parts if p).strip()
            if text:
                normalized.append(NormalizedMessage(role="assistant", content=text, timestamp=ts))
            normalized.extend(tool_messages)

        elif rec_type == "user":
            content_list = record.get("message", {}).get("content", [])
            if not isinstance(content_list, list):
                continue
            for item in content_list:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_result":
                    result_content = item.get("content", "")
                    if isinstance(result_content, list):
                        result_str = " ".join(
                            c.get("text", str(c)) if isinstance(c, dict) else str(c)
                            for c in result_content
                        )
                    else:
                        result_str = str(result_content) if result_content else ""
                    normalized.append(
                        NormalizedMessage(
                            role="tool_result",
                            content=result_str,
                            timestamp=ts,
                            tool_result=result_str,
                            is_error=bool(item.get("is_error")),
                        )
                    )
                elif item.get("type") == "text":
                    text = item.get("text", "")
                    if text:
                        normalized.append(
                            NormalizedMessage(role="user", content=text, timestamp=ts)
                        )

        elif rec_type == "result":
            # Final result record — emit as system message if it has content
            result = record.get("result", "")
            if result and isinstance(result, str):
                normalized.append(NormalizedMessage(role="system", content=result, timestamp=ts))

    return normalized


def _normalize_codex(msgs: list[dict]) -> list[NormalizedMessage]:
    """Normalize Codex CLI .jsonl messages.

    Codex format uses typed records:
    - ``response_item`` with payload.type == ``"message"``: assistant/user text
    - ``response_item`` with payload.type == ``"function_call"``: tool call
    - ``response_item`` with payload.type == ``"function_call_output"``: tool result
    """
    normalized: list[NormalizedMessage] = []

    for record in msgs:
        rec_type = record.get("type", "")
        ts = _ts_str(_parse_timestamp(record.get("timestamp", "")))

        if rec_type == "response_item":
            payload = record.get("payload") or {}
            payload_type = payload.get("type", "")

            if payload_type == "message":
                role = payload.get("role", "assistant")
                # content can be a list of content blocks or a plain string
                content_raw = payload.get("content", "")
                if isinstance(content_raw, list):
                    text_parts = [
                        c.get("text", "") if isinstance(c, dict) else str(c) for c in content_raw
                    ]
                    content = "\n".join(p for p in text_parts if p).strip()
                else:
                    content = str(content_raw) if content_raw else ""
                if role not in ("user", "assistant", "system"):
                    role = "assistant"
                normalized.append(NormalizedMessage(role=role, content=content, timestamp=ts))

            elif payload_type == "function_call":
                tool_name = payload.get("name", "")
                args = payload.get("arguments", "")
                if isinstance(args, str):
                    try:
                        tool_input: dict = json.loads(args)
                    except (json.JSONDecodeError, ValueError):
                        tool_input = {"raw": args}
                else:
                    tool_input = args if isinstance(args, dict) else {}
                normalized.append(
                    NormalizedMessage(
                        role="assistant",
                        content="",
                        timestamp=ts,
                        tool_name=tool_name,
                        tool_input=tool_input,
                    )
                )

            elif payload_type == "function_call_output":
                output = payload.get("output") or ""
                normalized.append(
                    NormalizedMessage(
                        role="tool_result",
                        content=str(output),
                        timestamp=ts,
                        tool_result=str(output),
                    )
                )

    return normalized


def _normalize_copilot(msgs: list[dict]) -> list[NormalizedMessage]:
    """Normalize Copilot CLI events.jsonl messages.

    Copilot format uses typed events:
    - ``assistant.message``: agent turn with toolRequests[]
    - ``tool.execution_complete``: tool result
    - ``user.message``: user input (if present)
    """
    normalized: list[NormalizedMessage] = []

    for record in msgs:
        rec_type = record.get("type", "")
        ts = _ts_str(_parse_timestamp(record.get("timestamp", "")))
        data = record.get("data") or {}

        if rec_type == "assistant.message":
            text = data.get("text", "") or ""
            tool_requests = data.get("toolRequests") or []
            if text:
                normalized.append(NormalizedMessage(role="assistant", content=text, timestamp=ts))
            for req in tool_requests:
                tool_name = req.get("name", "")
                args = req.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        tool_input = json.loads(args)
                    except (json.JSONDecodeError, ValueError):
                        tool_input = {"raw": args}
                else:
                    tool_input = args if isinstance(args, dict) else {}
                normalized.append(
                    NormalizedMessage(
                        role="assistant",
                        content="",
                        timestamp=ts,
                        tool_name=tool_name,
                        tool_input=tool_input,
                    )
                )

        elif rec_type == "tool.execution_complete":
            success = data.get("success", True)
            result = data.get("result") or {}
            content = result.get("detailedContent", "") or result.get("content", "") or ""
            if isinstance(content, list):
                content = " ".join(str(c) for c in content)
            normalized.append(
                NormalizedMessage(
                    role="tool_result",
                    content=str(content),
                    timestamp=ts,
                    tool_result=str(content),
                    is_error=not success,
                )
            )

        elif rec_type == "user.message":
            text = data.get("text", "") or ""
            if text:
                normalized.append(NormalizedMessage(role="user", content=text, timestamp=ts))

    return normalized


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def read_transcript(path: Path) -> SessionTranscript:
    """Read a trajectory file and return a normalized SessionTranscript.

    Auto-detects the harness format (gptme, claude-code, codex, copilot).
    The ``messages`` list is in chronological order as they appear in the
    source file — no resorting is applied.

    Parameters
    ----------
    path:
        Path to a harness JSONL file (conversation.jsonl, session UUID.jsonl,
        codex rollout.jsonl, or copilot events.jsonl).

    Returns
    -------
    SessionTranscript
        Normalized transcript with schema_version=1.
    """
    msgs = parse_trajectory(path)
    fmt = detect_format(msgs)

    # Map internal detect_format names to harness names
    harness_map = {
        "gptme": "gptme",
        "claude_code": "claude-code",
        "codex": "codex",
        "copilot": "copilot",
    }
    harness = harness_map.get(fmt, fmt)

    # Normalize messages
    if fmt == "claude_code":
        norm_msgs = _normalize_cc(msgs)
    elif fmt == "codex":
        norm_msgs = _normalize_codex(msgs)
    elif fmt == "copilot":
        norm_msgs = _normalize_copilot(msgs)
    else:
        norm_msgs = _normalize_gptme(msgs)

    # Extract timestamps for started_at / last_activity
    timestamps: list[datetime] = []
    for nm in norm_msgs:
        if nm.timestamp:
            ts = _parse_timestamp(nm.timestamp)
            if ts is not None:
                timestamps.append(ts)

    started_at = _ts_str(min(timestamps)) if timestamps else None
    last_activity = _ts_str(max(timestamps)) if timestamps else None

    # Extract session metadata from path
    session_name = extract_session_name(harness, path)
    project = extract_project(harness, path)

    # Model extraction: for CC we can read from the first assistant message
    model: str | None = None
    if fmt == "claude_code":
        from .discovery import extract_cc_model

        model = extract_cc_model(path)
    elif fmt == "gptme":
        # Check parent dir config.toml
        session_dir = path.parent if path.suffix == ".jsonl" else path
        from .discovery import parse_gptme_config

        model = parse_gptme_config(session_dir).get("model") or None
    # For codex/copilot, model is in usage/context records (not extracted here to keep lean)

    # Session ID: use path stem (UUID for CC, session dir name for gptme, etc.)
    session_id = path.stem if path.suffix == ".jsonl" else path.name

    capabilities: list[str] = []
    if norm_msgs:
        capabilities.append("view_transcript")

    return SessionTranscript(
        schema_version=TRANSCRIPT_SCHEMA_VERSION,
        session_id=session_id,
        harness=harness,
        session_name=session_name,
        project=project,
        model=model,
        started_at=started_at,
        last_activity=last_activity,
        trajectory_path=str(path.resolve()),
        capabilities=capabilities,
        messages=norm_msgs,
    )
