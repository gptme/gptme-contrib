"""Normalized session transcript for external consumers.

Reads raw harness-specific JSONL files and produces a stable, version-tagged
JSON contract that external consumers (dashboards, fleet operators, analysis
tools) can depend on without parsing harness-specific formats directly.

Supported harnesses: gptme, claude-code, codex, copilot, grok, pi.

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
from .pi import active_pi_records, pi_content_text
from .signals import _parse_timestamp, detect_format, extract_usage_pi, parse_trajectory

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
    tool_call_id : str | None
        Harness call identifier linking a tool invocation and its result.
    is_reasoning : bool
        True for reasoning deltas, separate from user-facing assistant text.
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
    tool_call_id: str | None = None
    is_reasoning: bool = False

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
        One of ``"gptme"``, ``"claude-code"``, ``"codex"``, ``"copilot"``,
        ``"grok"``, or ``"pi"``.
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
    provider : str | None
        Provider recorded by the active Pi branch, when available.
    stop_reason : str | None
        Final assistant stop reason on the active Pi branch, when available.
    usage : dict | None
        Exact normalized Pi token/cache/cost metadata, when available.
    cost : float | None
        Exact total session cost recorded by Pi, when available.
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
    provider: str | None = None
    stop_reason: str | None = None
    usage: dict[str, object] | None = None
    cost: float | None = None

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


def _codex_output_text(output: object) -> tuple[str, bool]:
    """Unwrap exec JSON chunks without dropping plain-text tool results."""
    lines: list[str] = []
    is_error = False
    if isinstance(output, list):
        # Current codex-cli: a list of input_text blocks, headed by
        # "Script completed|failed" and followed by exec JSON chunks.
        output = "\n".join(
            str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in output
        )
        is_error = output.startswith("Script failed")
    for line in str(output).splitlines():
        try:
            chunk = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            lines.append(line)
            continue
        if not isinstance(chunk, dict):
            lines.append(line)
            continue
        inner = chunk.get("value")
        if not isinstance(inner, dict):
            inner = chunk
        if "output" not in inner:
            lines.append(line)
            continue
        lines.append(str(inner["output"]))
        exit_code = inner.get("exit_code")
        is_error |= isinstance(exit_code, int) and exit_code != 0
    return "\n".join(lines), is_error


def _normalize_codex(msgs: list[dict]) -> list[NormalizedMessage]:
    """Normalize Codex CLI .jsonl messages.

    Codex format uses typed records:
    - ``response_item`` with payload.type == ``"message"``: assistant/user text
    - ``response_item`` with payload.type in function/custom_tool_call: tool call
    - ``response_item`` with payload.type in function/custom_tool_call_output: tool result
    Developer messages map to system instructions, never assistant actions.
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
                if role == "developer":
                    role = "system"
                elif role not in ("user", "assistant", "system"):
                    role = "assistant"
                normalized.append(NormalizedMessage(role=role, content=content, timestamp=ts))

            elif payload_type in ("function_call", "custom_tool_call"):
                tool_name = payload.get("name", "")
                args = (
                    payload.get("input", "")
                    if payload_type == "custom_tool_call"
                    else payload.get("arguments", "")
                )
                if isinstance(args, str):
                    try:
                        decoded = json.loads(args)
                        tool_input: dict = decoded if isinstance(decoded, dict) else {"raw": args}
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
                        tool_call_id=payload.get("call_id") or None,
                    )
                )

            elif payload_type in ("function_call_output", "custom_tool_call_output"):
                output, is_error = _codex_output_text(payload.get("output") or "")
                normalized.append(
                    NormalizedMessage(
                        role="tool_result",
                        content=output,
                        timestamp=ts,
                        tool_result=output,
                        is_error=is_error,
                        tool_call_id=payload.get("call_id") or None,
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


def _normalize_pi(msgs: list[dict]) -> list[NormalizedMessage]:
    """Normalize the active branch of a Pi native v3 tree session."""
    normalized: list[NormalizedMessage] = []

    for entry in active_pi_records(msgs):
        entry_type = entry.get("type")
        ts = _ts_str(_parse_timestamp(entry.get("timestamp", "")))

        if entry_type in ("compaction", "branch_summary"):
            summary = entry.get("summary")
            if isinstance(summary, str) and summary:
                normalized.append(NormalizedMessage(role="system", content=summary, timestamp=ts))
            continue

        if entry_type == "custom_message":
            content = pi_content_text(entry.get("content"))
            if content:
                normalized.append(NormalizedMessage(role="system", content=content, timestamp=ts))
            continue

        if entry_type != "message":
            continue
        message = entry.get("message") or {}
        if not isinstance(message, dict):
            continue
        role = message.get("role")

        if role == "user":
            content = pi_content_text(message.get("content"))
            if content:
                normalized.append(NormalizedMessage(role="user", content=content, timestamp=ts))

        elif role == "assistant":
            content_blocks = message.get("content")
            if not isinstance(content_blocks, list):
                continue
            text = pi_content_text(content_blocks).strip()
            if text:
                normalized.append(NormalizedMessage(role="assistant", content=text, timestamp=ts))
            for block in content_blocks:
                if not isinstance(block, dict) or block.get("type") != "toolCall":
                    continue
                tool_name = block.get("name")
                if not isinstance(tool_name, str) or not tool_name:
                    continue
                arguments = block.get("arguments")
                normalized.append(
                    NormalizedMessage(
                        role="assistant",
                        content="",
                        timestamp=ts,
                        tool_name=tool_name,
                        tool_input=arguments if isinstance(arguments, dict) else {},
                    )
                )

        elif role == "toolResult":
            content = pi_content_text(message.get("content"))
            normalized.append(
                NormalizedMessage(
                    role="tool_result",
                    content=content,
                    timestamp=ts,
                    tool_result=content,
                    is_error=message.get("isError") is True,
                )
            )

        elif role == "bashExecution":
            command = message.get("command")
            output = message.get("output")
            normalized.append(
                NormalizedMessage(
                    role="assistant",
                    content="",
                    timestamp=ts,
                    tool_name="bash",
                    tool_input={"command": command} if isinstance(command, str) else {},
                )
            )
            output_text = output if isinstance(output, str) else ""
            exit_code = message.get("exitCode")
            normalized.append(
                NormalizedMessage(
                    role="tool_result",
                    content=output_text,
                    timestamp=ts,
                    tool_result=output_text,
                    is_error=(isinstance(exit_code, int) and exit_code != 0)
                    or message.get("cancelled") is True,
                )
            )

        elif role == "custom":
            content = pi_content_text(message.get("content"))
            if content:
                normalized.append(NormalizedMessage(role="system", content=content, timestamp=ts))

    return normalized


def _grok_output(raw: dict) -> tuple[str, bool]:
    """Read inline, polled, or variant output, including nested exit status."""
    output = raw.get("output_for_prompt") or raw.get("output") or ""
    exit_code = raw.get("exit_code")
    is_error = isinstance(exit_code, int) and exit_code != 0
    result = raw.get("Result")
    results = result if isinstance(result, list) else [result]
    parts = []
    for item in results:
        if not isinstance(item, dict):
            continue
        text, failed = _grok_output(item)
        if text:
            parts.append(text)
        is_error |= failed
    output = output or "\n".join(parts)
    if not output:
        # Variant payloads such as TodosUpdated carry a summary next to
        # structured state. Only use a single direct string field; descending
        # into state or choosing among several strings would invent output.
        candidates = [
            value
            for key, variant in raw.items()
            if key != "Result" and isinstance(variant, dict)
            for value in variant.values()
            if isinstance(value, str)
        ]
        if len(candidates) == 1:
            output = candidates[0]
    return str(output), is_error


def _normalize_grok(msgs: list[dict]) -> list[NormalizedMessage]:
    """Normalize Grok deltas and consolidate updates by toolCallId.

    Results retain their first update position; later snapshots enrich that
    result. Completed inline output replaces progress, even when shorter.
    Background output can arrive after the completed BackgroundTaskStarted
    envelope or through a later TaskOutput poll.
    """
    normalized: list[NormalizedMessage] = []
    results: dict[str, NormalizedMessage] = {}
    background: dict[str, str] = {}
    background_calls: set[str] = set()
    background_pending: set[str] = set()
    finalized: set[str] = set()
    delta_type: str | None = None

    def put_result(
        call_id: str,
        text: str,
        ts: str | None,
        is_error: bool,
        authoritative: bool,
        final: bool = False,
    ) -> None:
        previous = results.get(call_id) if call_id else None
        if previous is None:
            previous = NormalizedMessage(
                role="tool_result",
                content=text,
                timestamp=ts,
                tool_result=text,
                tool_call_id=call_id or None,
                is_error=is_error,
            )
            normalized.append(previous)
            if call_id:
                results[call_id] = previous
        else:
            if authoritative:
                previous.content = text
                previous.tool_result = text
            elif call_id not in finalized and len(text) > len(previous.content):
                # A late progress snapshot may enrich a non-final result, but it
                # must never overwrite output already finalized by a completion.
                previous.content = text
                previous.tool_result = text
            # An authoritative snapshot supersedes every prior flag; a
            # non-authoritative progress snapshot may only add an error.
            previous.is_error = is_error if authoritative else (previous.is_error or is_error)
        if final and call_id:
            finalized.add(call_id)

    for record in msgs:
        rec_type = record.get("type", "")
        ts = _ts_str(_parse_timestamp(record.get("timestamp", "")))
        if rec_type in ("thought", "text"):
            content = record.get("data") or record.get("content") or ""
            if content:
                if delta_type == rec_type:
                    normalized[-1].content += str(content)
                else:
                    normalized.append(
                        NormalizedMessage(
                            role="assistant",
                            content=str(content),
                            timestamp=ts,
                            is_reasoning=rec_type == "thought",
                        )
                    )
                delta_type = rec_type
            continue
        delta_type = None

        if rec_type == "error":
            message = record.get("message") or record.get("data") or record.get("content")
            if message:
                normalized.append(
                    NormalizedMessage(
                        role="system",
                        content=str(message),
                        timestamp=ts,
                        is_error=True,
                    )
                )
        elif rec_type == "tool_call":
            tool_name = record.get("toolName", "")
            raw_input = record.get("rawInput") or {}
            if tool_name:
                normalized.append(
                    NormalizedMessage(
                        role="assistant",
                        content="",
                        timestamp=ts,
                        tool_name=tool_name,
                        tool_input=raw_input if isinstance(raw_input, dict) else {},
                        tool_call_id=record.get("toolCallId") or None,
                    )
                )
        elif rec_type == "tool_call_update":
            status = record.get("status")
            if status not in ("completed", "in_progress"):
                continue
            call_id = record.get("toolCallId") or ""
            raw = record.get("rawOutput") or {}
            if not isinstance(raw, dict):
                raw = {}
            output, is_error = _grok_output(raw)
            parts = []
            for block in record.get("content") or []:
                inner = block.get("content") if isinstance(block, dict) else None
                if isinstance(inner, dict) and inner.get("text"):
                    parts.append(str(inner["text"]))
            text = "\n".join(parts) or output
            if raw.get("type") == "BackgroundTaskStarted":
                task_id = raw.get("task_id")
                if task_id and call_id:
                    background[str(task_id)] = call_id
                    background_calls.add(call_id)
                    background_pending.add(call_id)
            if text or status == "completed":
                # Background output streams as a series of growing, cumulative
                # ``in_progress`` snapshots (empirically ~38.7k growing vs 770
                # shrinking transitions across the corpus; background calls never
                # emit a ``completed`` update). ``first_background_output`` is the
                # *first* partial snapshot, not the final output, so it must stay
                # non-final: later, longer snapshots legitimately enrich it. Only
                # an explicit completion (or a TaskOutput restore) is final.
                first_background_output = (
                    bool(text)
                    and call_id in background_pending
                    and raw.get("type") != "BackgroundTaskStarted"
                )
                is_final = status == "completed" and call_id not in background_calls
                put_result(
                    call_id,
                    text,
                    ts,
                    is_error,
                    first_background_output or is_final,
                    # Only a completion that actually carries output finalizes
                    # the result. Grok also emits empty ``completed`` envelopes
                    # (e.g. Monitor calls) whose real output arrives afterwards
                    # as ``in_progress``; those must stay enrichable.
                    is_final and bool(text),
                )
                if first_background_output:
                    background_pending.discard(call_id)
            if raw.get("type") == "TaskOutput":
                result = raw.get("Result")
                items = result if isinstance(result, list) else [result]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    original_id = background.get(str(item.get("task_id", "")))
                    if original_id:
                        task_text, failed = _grok_output(item)
                        if task_text:
                            # A poll restores authoritative output but is not
                            # a terminal state: later, longer in_progress
                            # snapshots for the original call may still enrich
                            # it, so do not finalize here.
                            put_result(original_id, task_text, ts, failed, True)
    return normalized


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _extract_model(fmt: str, msgs: list[dict], path: Path) -> str | None:
    """Extract a model string for the detected harness when possible."""
    if fmt == "claude_code":
        from .discovery import extract_cc_model

        return extract_cc_model(path)

    if fmt == "gptme":
        session_dir = path.parent if path.suffix == ".jsonl" else path
        from .discovery import parse_gptme_config

        return parse_gptme_config(session_dir).get("model") or None

    if fmt == "codex":
        for record in msgs:
            if record.get("type") != "session_meta":
                continue
            payload = record.get("payload") or {}
            model = payload.get("model")
            if model:
                return str(model)
        return None

    if fmt == "copilot":
        for record in msgs:
            if record.get("type") != "session.start":
                continue
            data = record.get("data") or {}
            model = data.get("selectedModel")
            if model:
                return str(model)
        return None

    if fmt == "grok":
        for record in reversed(msgs):
            if record.get("type") != "end":
                continue
            model_usage = record.get("modelUsage") or {}
            model_name = next(iter(model_usage), None)
            if model_name:
                return str(model_name)
        return None

    return None


def read_transcript(path: Path) -> SessionTranscript:
    """Read a trajectory file and return a normalized SessionTranscript.

    Auto-detects the harness format (gptme, claude-code, codex, copilot, grok, pi).
    The ``messages`` list is in chronological order as they appear in the
    source file — no resorting is applied.

    Parameters
    ----------
    path:
        Path to a harness JSONL file (conversation.jsonl, session UUID.jsonl,
        codex rollout.jsonl, or copilot events.jsonl), or a gptme session
        directory containing ``conversation.jsonl``.

    Returns
    -------
    SessionTranscript
        Normalized transcript with schema_version=1.
    """
    # gptme sessions are directories; resolve to the JSONL file inside
    if path.is_dir():
        jsonl = path / "conversation.jsonl"
        if jsonl.exists():
            path = jsonl
        else:
            raise FileNotFoundError(
                f"{path} is a directory and does not contain conversation.jsonl"
            )
    msgs = parse_trajectory(path)
    fmt = detect_format(msgs)

    # Map internal detect_format names to harness names
    harness_map = {
        "gptme": "gptme",
        "claude_code": "claude-code",
        "codex": "codex",
        "copilot": "copilot",
        "grok": "grok",
        "pi": "pi",
    }
    harness = harness_map.get(fmt, fmt)

    # Normalize messages
    if fmt == "claude_code":
        norm_msgs = _normalize_cc(msgs)
    elif fmt == "pi":
        norm_msgs = _normalize_pi(msgs)
    elif fmt == "codex":
        norm_msgs = _normalize_codex(msgs)
    elif fmt == "copilot":
        norm_msgs = _normalize_copilot(msgs)
    elif fmt == "grok":
        norm_msgs = _normalize_grok(msgs)
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

    pi_usage: dict[str, object] | None = None
    pi_active_records: list[dict] | None = None
    provider: str | None = None
    stop_reason: str | None = None
    cost: float | None = None
    if fmt == "pi":
        pi_active_records = active_pi_records(msgs)
        header = pi_active_records[0]
        project = str(header.get("cwd")) if header.get("cwd") else None
        for entry in pi_active_records:
            if entry.get("type") == "session_info" and isinstance(entry.get("name"), str):
                session_name = entry["name"]
        pi_usage = extract_usage_pi(msgs)
        raw_provider = pi_usage.get("provider")
        provider = str(raw_provider) if raw_provider else None
        raw_stop_reason = pi_usage.get("stop_reason")
        stop_reason = str(raw_stop_reason) if raw_stop_reason else None
        raw_cost = pi_usage.get("cost")
        cost = float(raw_cost) if isinstance(raw_cost, (int, float)) else None

    if pi_usage is not None:
        raw_model = pi_usage.get("model")
        model = str(raw_model) if raw_model else None
    else:
        model = _extract_model(fmt, msgs, path)

    # Session ID: use path stem (UUID for CC, session dir name for gptme, etc.)
    # For gptme directories, path.stem will be "conversation" after conversion,
    # so use the parent directory name instead.
    if fmt == "pi":
        assert pi_active_records is not None
        session_id = str(pi_active_records[0]["id"])
    elif path.suffix == ".jsonl" and fmt == "gptme":
        session_id = path.parent.name
    else:
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
        provider=provider,
        stop_reason=stop_reason,
        usage=pi_usage,
        cost=cost,
        trajectory_path=str(path.resolve()),
        capabilities=capabilities,
        messages=norm_msgs,
    )


# ---------------------------------------------------------------------------
# Subagent tree resolution
# ---------------------------------------------------------------------------


@dataclass
class SubagentNode:
    """A resolved subagent transcript with provenance.

    Fields
    ------
    session_id : str
        Child session id (the ``agent-<id>`` file stem).
    agent_type : str | None
        ``agentType`` from the child's ``.meta.json`` (e.g. ``"Explore"``,
        ``"workflow-subagent"``), falling back to the parent tool's
        ``subagent_type``.
    description : str | None
        Task description from ``.meta.json`` or the parent tool input.
    spawn_depth : int
        Nesting depth. ``1`` is a direct child of the parent session.
    tool_use_id : str | None
        The parent's ``tool_use.id`` that spawned this child (``None`` for
        workflow agents, which carry no tool-use link).
    trajectory_path : str
        Absolute path to the child JSONL file.
    transcript : SessionTranscript
        Normalized transcript of the child session.
    records : list[dict]
        Raw harness JSONL records of the child (for signal extraction).
    children : list[SubagentNode]
        Nested subagents spawned by this child.
    """

    session_id: str
    trajectory_path: str
    transcript: SessionTranscript
    records: list[dict] = field(default_factory=list)
    children: list["SubagentNode"] = field(default_factory=list)
    agent_type: str | None = None
    description: str | None = None
    spawn_depth: int = 1
    tool_use_id: str | None = None


@dataclass
class SessionTree:
    """A session with its subagents resolved into a tree.

    ``parent`` is the root transcript. ``subagents`` holds the direct
    subagents (spawn depth 1), each of which may itself have ``children``.
    """

    parent: SessionTranscript
    parent_records: list[dict] = field(default_factory=list)
    subagents: list[SubagentNode] = field(default_factory=list)

    @property
    def total_subagents(self) -> int:
        """Total number of subagent nodes in the tree (all depths)."""

        def count(nodes: list[SubagentNode]) -> int:
            return sum(1 + count(n.children) for n in nodes)

        return count(self.subagents)

    def flatten_records(self) -> list[dict]:
        """Raw JSONL records: parent first, then all descendants pre-order.

        Suitable for feeding harness signal extractors so subagent tool calls,
        tokens, file writes and commits count toward the parent session.
        """
        out = list(self.parent_records)

        def walk(nodes: list[SubagentNode]) -> None:
            for n in nodes:
                out.extend(n.records)
                walk(n.children)

        walk(self.subagents)
        return out

    def flatten_messages(self) -> list[NormalizedMessage]:
        """Normalized messages: parent first, then all descendants pre-order."""
        out = list(self.parent.messages)

        def walk(nodes: list[SubagentNode]) -> None:
            for n in nodes:
                out.extend(n.transcript.messages)
                walk(n.children)

        walk(self.subagents)
        return out


def _resolve_parent_jsonl(path: Path) -> Path:
    """Resolve a session path (directory or JSONL file) to its parent JSONL."""
    if path.is_dir():
        jsonl = path / "conversation.jsonl"
        if jsonl.exists():
            return jsonl
        raise FileNotFoundError(f"{path} is a directory and does not contain conversation.jsonl")
    return path


def _find_subagents_dir(parent_jsonl: Path) -> Path | None:
    """Locate the ``subagents/`` directory for a session, or ``None``.

    Claude Code stores subagents under ``<session-id>/subagents/`` next to the
    ``<session-id>.jsonl`` parent. Also covers legacy session-local layouts;
    native gptme siblings are resolved separately through parent_logdir.
    """
    candidates = (
        parent_jsonl.parent / parent_jsonl.stem / "subagents",  # claude-code
        parent_jsonl.parent / "subagents",  # gptme session dir
    )
    for c in candidates:
        if c.is_dir():
            return c
    return None


def subagent_record_files(path: Path) -> list[Path]:
    """JSONL files of every subagent spawned by the session at ``path``.

    Accepts a JSONL file or a gptme session directory. Covers both the flat
    ``agent-*.jsonl`` agents (nested ones included), workflow agents under
    ``workflows/*/``, and native gptme sibling directories with explicit
    ``subagent-meta.json.parent_logdir``. No name/mtime inference is used.
    """
    parent_jsonl = _resolve_parent_jsonl(path)
    sub_dir = _find_subagents_dir(parent_jsonl)
    files = []
    if sub_dir is not None:
        files = sorted(sub_dir.glob("agent-*.jsonl"))
        files += sorted(sub_dir.glob("workflows/*/agent-*.jsonl"))
    files += [file for file, _meta, _depth in _native_subagents(parent_jsonl)]
    return files


def subagent_record_metadata(path: Path) -> list[tuple[Path, dict, int]]:
    """Resolved transcripts, provenance and depth for signal extraction."""
    parent_jsonl = _resolve_parent_jsonl(path)
    native = _native_subagents(parent_jsonl)
    native_paths = {file for file, _meta, _depth in native}
    out = []
    for file in subagent_record_files(parent_jsonl):
        if file in native_paths:
            continue
        meta = read_subagent_metadata(file)
        try:
            depth = int(meta.get("spawnDepth") or 1)
        except (TypeError, ValueError):
            depth = 1
        out.append((file, meta, depth))
    return out + native


def read_subagent_metadata(path: Path) -> dict:
    """Read child provenance in either CC or native gptme layout."""
    native = path.name == "conversation.jsonl"
    meta_path = path.parent / "subagent-meta.json" if native else path.with_suffix(".meta.json")
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(meta, dict):
        return {}
    if native:
        return {
            **meta,
            "agentType": meta.get("profile") or meta.get("agent_type"),
            "description": meta.get("prompt"),
            "session_id": meta.get("agent_id") or path.parent.name,
        }
    return meta


def _native_subagents(parent_jsonl: Path) -> list[tuple[Path, dict, int]]:
    """Resolve native siblings by exact parent identity, never time/name proximity.

    Scan only the parent's logs root. Retained roots and legacy roots share the
    same layout; an absent/invalid parent_logdir is intentionally not inferred.
    """
    if parent_jsonl.name != "conversation.jsonl":
        return []
    by_parent: dict[Path, list[tuple[Path, dict]]] = {}
    for meta_path in sorted(parent_jsonl.parent.parent.glob("subagent-*/subagent-meta.json")):
        file = meta_path.parent / "conversation.jsonl"
        if not file.is_file():
            continue
        meta = read_subagent_metadata(file)
        identity = meta.get("parent_logdir")
        if not isinstance(identity, str) or not identity or not Path(identity).is_absolute():
            continue
        by_parent.setdefault(Path(identity).resolve(), []).append((file, meta))
    out: list[tuple[Path, dict, int]] = []
    visited = {parent_jsonl.resolve()}

    def walk(parent: Path, depth: int) -> None:
        for file, meta in by_parent.get(parent.resolve(), []):
            resolved = file.resolve()
            if resolved in visited:
                continue
            visited.add(resolved)
            out.append((file, meta, depth))
            walk(file.parent, depth + 1)

    walk(parent_jsonl.parent, 1)
    return out


def _agent_tool_use_ids(records: list[dict]) -> list[tuple[str, dict]]:
    """Return ``(tool_use_id, block)`` for each ``Agent`` tool use in records."""
    out: list[tuple[str, dict]] = []
    for r in records:
        if r.get("type") != "assistant":
            continue
        content = r.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == "Agent"
            ):
                tool_id = block.get("id")
                if tool_id is not None:
                    out.append((tool_id, block))
    return out


def _make_subagent_node(
    child_path: Path,
    meta: dict,
    tool_use_id: str | None,
    parent_depth: int,
    agent_map: dict[str, tuple[dict, Path]],
    visited: set[str],
) -> SubagentNode:
    """Build a SubagentNode for ``child_path``, recursing into its children."""
    child_records = parse_trajectory(child_path)
    visited.add(str(child_path))
    return SubagentNode(
        session_id=child_path.stem,
        agent_type=meta.get("agentType"),
        description=meta.get("description"),
        spawn_depth=int(meta.get("spawnDepth", parent_depth + 1) or parent_depth + 1),
        tool_use_id=tool_use_id,
        trajectory_path=str(child_path),
        transcript=read_transcript(child_path),
        records=child_records,
        children=_build_subagent_nodes(child_records, agent_map, parent_depth + 1, visited),
    )


def _build_subagent_nodes(
    records: list[dict],
    agent_map: dict[str, tuple[dict, Path]],
    parent_depth: int,
    visited: set[str],
) -> list[SubagentNode]:
    """Resolve the subagents whose tool-use ids appear in ``records``.

    ``visited`` guards against self-loops: a subagent's JSONL carries the
    parent's ``Agent`` tool-use as context, so its own ``toolUseId`` re-matches
    the node itself. Skipping already-visited children also breaks any
    cross-referential cycle.
    """
    nodes: list[SubagentNode] = []
    for tool_use_id, block in _agent_tool_use_ids(records):
        entry = agent_map.get(tool_use_id)
        if entry is None:
            continue
        meta, child_path = entry
        if str(child_path) in visited:
            continue
        nodes.append(
            _make_subagent_node(child_path, meta, tool_use_id, parent_depth, agent_map, visited)
        )
    return nodes


def read_session_tree(path: Path) -> SessionTree:
    """Read a session and resolve its subagent tree.

    The parent transcript is returned as ``tree.parent``. Each ``Agent`` tool
    call whose ``tool_use.id`` matches a subagent ``.meta.json`` ``toolUseId``
    is expanded into a :class:`SubagentNode`, recursively by ``spawnDepth``.
    Workflow agents and flat agents without readable metadata (no
    ``toolUseId``) are attached as direct children of the parent.

    A session without subagents returns a tree with an empty ``subagents``
    list whose ``flatten_records`` / ``flatten_messages`` are identical to the
    parent transcript alone.
    """
    parent_jsonl = _resolve_parent_jsonl(path)
    parent_records = parse_trajectory(parent_jsonl)
    parent = read_transcript(parent_jsonl)

    agent_map: dict[str, tuple[dict, Path]] = {}
    workflow_files: list[Path] = []
    sub_dir = _find_subagents_dir(parent_jsonl)
    if sub_dir is not None:
        for meta_path in sorted(sub_dir.glob("agent-*.meta.json")):
            try:
                meta = json.loads(meta_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            child_stem = meta_path.name.removesuffix(".meta.json")
            child_path = meta_path.parent / f"{child_stem}.jsonl"
            if not child_path.exists():
                continue
            tool_use_id = meta.get("toolUseId", "")
            if tool_use_id:
                agent_map[tool_use_id] = (meta, child_path)
            else:
                workflow_files.append(child_path)
        # Catch flat and workflow JSONL files whose .meta.json was absent or
        # unreadable. Without a tool-use join they are attached at the root,
        # preserving their work instead of silently dropping it.
        mapped = {c for _, c in agent_map.values()}
        fallback_files = sorted(sub_dir.glob("agent-*.jsonl"))
        wf_dir = sub_dir / "workflows"
        if wf_dir.is_dir():
            fallback_files += sorted(wf_dir.glob("*/agent-*.jsonl"))
        for child_path in fallback_files:
            if child_path not in mapped and child_path not in workflow_files:
                workflow_files.append(child_path)

    tree = SessionTree(parent=parent, parent_records=parent_records)
    visited: set[str] = set()
    tree.subagents = _build_subagent_nodes(parent_records, agent_map, 0, visited)
    for child_path in workflow_files:
        if str(child_path) in visited:
            continue
        tree.subagents.append(_make_subagent_node(child_path, {}, None, 0, agent_map, visited))
    native_nodes: dict[Path, SubagentNode] = {}
    for child_path, meta, depth in _native_subagents(parent_jsonl):
        if str(child_path) in visited:
            continue
        node = _make_subagent_node(child_path, meta, None, depth - 1, {}, visited)
        node.session_id = str(meta["session_id"])
        node.spawn_depth = depth
        native_nodes[child_path.parent.resolve()] = node
        parent_node = native_nodes.get(Path(meta["parent_logdir"]).resolve())
        if parent_node is not None:
            parent_node.children.append(node)
        else:
            tree.subagents.append(node)
    return tree
