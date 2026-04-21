"""Per-tool-call span extraction from agent trajectory files.

A ToolSpan is a single tool invocation: one tool called once, with its
timing, input/output sizes, and success recorded. Sessions produce
sequences of spans that tell the per-turn story of what the agent did
and how long each operation took.

Supports Claude Code JSONL format. gptme format planned for a follow-up.

Design doc: knowledge/technical-designs/span-level-tracing-design.md
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_EXIT_CODE_RE = re.compile(r"(?:Exit code|exit code):\s*(\d+)")


def _parse_ts(ts_str: str | None) -> datetime | None:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def _input_size(tool_input: object) -> int:
    if isinstance(tool_input, dict):
        return sum(len(str(v)) for v in tool_input.values())
    return len(str(tool_input))


def _output_size(content: object) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(c.get("text", str(c))) if isinstance(c, dict) else len(str(c)) for c in content
        )
    return 0


def _output_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", str(c)) if isinstance(c, dict) else str(c) for c in content)
    return str(content)


def _exit_code(content: object) -> int | None:
    text = _output_text(content)
    m = _EXIT_CODE_RE.search(text)
    return int(m.group(1)) if m else None


@dataclass
class ToolSpan:
    """A single tool invocation within an agent session.

    Attributes:
        span_id: Unique identifier for this span (UUID).
        session_id: Parent session ID (trajectory filename stem by default).
        tool_name: Tool that was invoked (e.g. "Bash", "Edit", "Read").
        timestamp: ISO 8601 dispatch time (when the assistant sent the call).
        duration_ms: Wall-clock milliseconds from dispatch to result arrival.
            -1 when timestamps are unavailable or out-of-order.
        success: False when the tool result carries ``is_error=True``.
        input_size: Character count of the tool's input parameters.
        output_size: Character count of the tool result content.
        exit_code: For Bash spans, the subprocess exit code when annotated
            in the result text ("Exit code: N"). None otherwise.
        turn_index: 0-indexed assistant turn that dispatched this tool call.
    """

    span_id: str
    session_id: str
    tool_name: str
    timestamp: str
    duration_ms: int
    success: bool
    input_size: int
    output_size: int
    exit_code: int | None
    turn_index: int
    matched_lessons: list[str] = field(default_factory=list)


@dataclass
class SpanAggregates:
    """Session-level aggregates derived from a list of ToolSpans.

    These fields are suitable for inclusion in SessionRecord as optional
    fields (Phase 2 of the design doc).

    Attributes:
        retry_depth: Max consecutive redundant re-calls to the same tool
            (first invocation excluded).  A value of 0 means no tool was
            called twice in a row; 2 means the same tool was called 3 times
            in succession (1 original + 2 retries).  Proxy for stuck loops.
    """

    total_spans: int
    error_spans: int
    dominant_tool: str | None
    avg_duration_ms: float
    max_duration_ms: int
    tool_counts: dict[str, int]
    retry_depth: int

    @property
    def error_rate(self) -> float:
        return self.error_spans / self.total_spans if self.total_spans else 0.0

    @classmethod
    def from_spans(cls, spans: list[ToolSpan]) -> SpanAggregates:
        if not spans:
            return cls(
                total_spans=0,
                error_spans=0,
                dominant_tool=None,
                avg_duration_ms=-1.0,
                max_duration_ms=-1,
                tool_counts={},
                retry_depth=0,
            )

        tool_counts: dict[str, int] = {}
        errors = 0
        known_durations: list[int] = []

        for span in spans:
            tool_counts[span.tool_name] = tool_counts.get(span.tool_name, 0) + 1
            if not span.success:
                errors += 1
            if span.duration_ms >= 0:
                known_durations.append(span.duration_ms)

        dominant = max(tool_counts, key=lambda k: tool_counts[k]) if tool_counts else None
        avg_ms = sum(known_durations) / len(known_durations) if known_durations else -1.0
        max_ms = max(known_durations) if known_durations else -1

        # Retry depth: max consecutive redundant re-calls to the same tool
        # (first call is normal; streak counts re-invocations beyond it)
        retry_depth = 0
        streak = 0
        for i in range(1, len(spans)):
            if spans[i].tool_name == spans[i - 1].tool_name:
                streak += 1
                retry_depth = max(retry_depth, streak)
            else:
                streak = 0

        return cls(
            total_spans=len(spans),
            error_spans=errors,
            dominant_tool=dominant,
            avg_duration_ms=avg_ms,
            max_duration_ms=max_ms,
            tool_counts=tool_counts,
            retry_depth=retry_depth,
        )


def extract_spans_from_cc_jsonl(
    path: Path | str,
    session_id: str | None = None,
) -> list[ToolSpan]:
    """Extract ToolSpan objects from a Claude Code JSONL trajectory file.

    Parses assistant tool_use dispatches and user tool_result arrivals,
    pairs them by tool_use_id, and computes per-span timing.

    Args:
        path: Path to the .jsonl trajectory file.
        session_id: Session ID to assign to all spans. Defaults to the
            filename stem (e.g. ``"abc123"`` for ``abc123.jsonl``).

    Returns:
        List of spans in chronological dispatch order.
    """
    path = Path(path)
    if session_id is None:
        session_id = path.stem

    # pending maps tool_use_id → (tool_name, dispatch_ts, dispatch_ts_str, input_size, turn_index)
    pending: dict[str, tuple[str, datetime | None, str, int, int]] = {}
    spans: list[ToolSpan] = []
    turn_index = 0

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        rec_type = record.get("type")
        ts = _parse_ts(record.get("timestamp"))
        ts_str = record.get("timestamp", "")

        if rec_type == "assistant":
            content = record.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            dispatched_this_turn = False
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_use":
                    continue
                tool_id = item.get("id", "")
                tool_name = item.get("name", "unknown")
                isize = _input_size(item.get("input", {}))
                if tool_id:
                    pending[tool_id] = (tool_name, ts, ts_str, isize, turn_index)
                    dispatched_this_turn = True
            if dispatched_this_turn:
                turn_index += 1

        elif rec_type == "user":
            content = record.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_result":
                    continue
                tool_use_id = item.get("tool_use_id", "")
                if tool_use_id not in pending:
                    continue
                tool_name, dispatch_ts, dispatch_ts_str, isize, tidx = pending.pop(tool_use_id)
                is_error = bool(item.get("is_error"))
                result_content = item.get("content", "")
                osize = _output_size(result_content)

                dur_ms = -1
                if dispatch_ts is not None and ts is not None:
                    delta = (ts - dispatch_ts).total_seconds()
                    if delta >= 0:
                        dur_ms = int(delta * 1000)

                exit_code = _exit_code(result_content) if tool_name == "Bash" else None

                spans.append(
                    ToolSpan(
                        span_id=str(uuid.uuid4()),
                        session_id=session_id,
                        tool_name=tool_name,
                        timestamp=dispatch_ts_str,
                        duration_ms=dur_ms,
                        success=not is_error,
                        input_size=isize,
                        output_size=osize,
                        exit_code=exit_code,
                        turn_index=tidx,
                    )
                )

    return spans
