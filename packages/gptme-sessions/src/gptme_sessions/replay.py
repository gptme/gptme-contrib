"""Replay helpers for human-readable session transcript inspection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from .record import SessionRecord
from .store import SessionStore
from .transcript import NormalizedMessage, SessionTranscript, read_transcript

ToolResultsMode = Literal["summary", "full", "hide"]


def resolve_session_record_prefix(records: list[SessionRecord], prefix: str) -> SessionRecord:
    """Resolve a session record from a full ID or prefix."""
    if not prefix:
        raise ValueError("Session ID must not be empty.")

    matches = [record for record in records if record.session_id.startswith(prefix)]
    if not matches:
        raise ValueError(
            f"No session found matching '{prefix}'. "
            "Run 'gptme-sessions query' to list available session IDs."
        )
    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous prefix '{prefix}' matches {len(matches)} sessions: "
            + ", ".join(record.session_id for record in matches)
            + ". Run 'gptme-sessions query' to list available session IDs."
        )
    return matches[0]


def resolve_replay_target(target: str, *, sessions_dir: Path) -> SessionTranscript:
    """Resolve *target* to a transcript via path or session-record ID prefix."""
    if not target:
        raise ValueError("Replay target must not be empty.")

    path = Path(target).expanduser()
    if path.exists():
        return read_transcript(path)

    store = SessionStore(sessions_dir=sessions_dir)
    record = resolve_session_record_prefix(store.load_all(), target)
    if not record.trajectory_path:
        raise ValueError(f"Session '{record.session_id}' has no trajectory_path; cannot replay it.")
    return read_transcript(Path(record.trajectory_path).expanduser())


def render_replay(
    transcript: SessionTranscript,
    *,
    raw_system: bool = False,
    show_tool_input: bool = False,
    tool_results: ToolResultsMode = "summary",
    tail: int | None = None,
) -> str:
    """Render a transcript for terminal replay."""
    messages = list(transcript.messages)
    collapsed_count = 0
    collapsed_bytes = 0

    if not raw_system:
        for message in messages:
            if message.role != "system":
                break
            collapsed_count += 1
            collapsed_bytes += len((message.content or "").encode("utf-8"))
        messages = messages[collapsed_count:]

    if tail is not None:
        messages = messages[-tail:]

    lines = [
        f"Session:       {transcript.session_id}",
        f"Harness:       {transcript.harness}",
    ]
    if transcript.session_name:
        lines.append(f"Session name:  {transcript.session_name}")
    if transcript.model:
        lines.append(f"Model:         {transcript.model}")
    if transcript.project:
        lines.append(f"Project:       {transcript.project}")
    if transcript.started_at:
        lines.append(f"Started at:    {transcript.started_at}")
    if transcript.last_activity:
        lines.append(f"Last activity: {transcript.last_activity}")
    lines.extend(
        [
            f"Messages:      {len(transcript.messages)}",
            f"Source:        {transcript.trajectory_path}",
        ]
    )

    if collapsed_count:
        noun = "message" if collapsed_count == 1 else "messages"
        lines.extend(
            [
                "",
                (
                    f"[system prelude collapsed: {collapsed_count} {noun}, "
                    f"{_format_bytes(collapsed_bytes)}]"
                ),
            ]
        )

    if messages:
        lines.append("")

    for idx, message in enumerate(messages):
        if idx:
            lines.append("")
        lines.extend(
            _render_message(
                message,
                show_tool_input=show_tool_input,
                tool_results=tool_results,
            )
        )

    return "\n".join(lines).rstrip() + "\n"


def _render_message(
    message: NormalizedMessage,
    *,
    show_tool_input: bool,
    tool_results: ToolResultsMode,
) -> list[str]:
    """Render a single normalized message."""
    if message.tool_name:
        lines = [f"TOOL CALL  {message.tool_name}"]
        if message.timestamp:
            lines.append(f"time: {message.timestamp}")
        if show_tool_input and message.tool_input is not None:
            lines.append(json.dumps(message.tool_input, indent=2, sort_keys=True))
        return lines

    if message.role == "tool_result":
        heading = "TOOL RESULT"
        if message.is_error:
            heading += "  [ERROR]"
        lines = [heading]
        if message.timestamp:
            lines.append(f"time: {message.timestamp}")
        if tool_results == "hide":
            lines.append("[hidden]")
        else:
            content = message.tool_result or message.content or ""
            if tool_results == "full":
                lines.append(content or "[empty]")
            else:
                lines.append(_summarize_text(content))
        return lines

    heading = message.role.upper()
    lines = [heading]
    if message.timestamp:
        lines.append(f"time: {message.timestamp}")
    lines.append(message.content or "[empty]")
    return lines


def _summarize_text(text: str, *, max_lines: int = 4, max_chars: int = 320) -> str:
    """Return a stable compact summary of tool output."""
    stripped = text.strip()
    if not stripped:
        return "[empty]"

    lines = stripped.splitlines()
    truncated = False
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True

    summary = "\n".join(lines)
    if len(summary) > max_chars:
        summary = summary[: max_chars - 3].rstrip()
        truncated = True

    if truncated:
        summary = summary.rstrip() + "\n..."
    return summary


def _format_bytes(byte_count: int) -> str:
    """Format a byte count for replay summaries."""
    if byte_count >= 1024:
        return f"{byte_count / 1024:.1f} KB"
    return f"{byte_count} B"
