"""Tests for span-level tracing (gptme_sessions.spans)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gptme_sessions.spans import (
    SpanAggregates,
    ToolSpan,
    extract_spans_from_cc_jsonl,
    extract_spans_from_gptme_jsonl,
)


def _write_jsonl(tmp_path: Path, records: list[dict], name: str = "session.jsonl") -> Path:
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


# ── CC JSONL fixtures ─────────────────────────────────────────────────────────


def _cc_assistant(tool_name: str, tool_id: str, cmd: str, ts: str) -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": tool_name,
                    "input": {"command": cmd},
                }
            ]
        },
    }


def _cc_result(tool_use_id: str, output: str, ts: str, is_error: bool = False) -> dict:
    return {
        "type": "user",
        "timestamp": ts,
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": output,
                    "is_error": is_error,
                }
            ]
        },
    }


# ── extract_spans_from_cc_jsonl ───────────────────────────────────────────────


def test_single_bash_span(tmp_path: Path) -> None:
    records = [
        _cc_assistant("Bash", "tid1", "echo hello", "2026-04-21T10:00:00+00:00"),
        _cc_result("tid1", "hello", "2026-04-21T10:00:01+00:00"),
    ]
    p = _write_jsonl(tmp_path, records)
    spans = extract_spans_from_cc_jsonl(p)

    assert len(spans) == 1
    s = spans[0]
    assert s.tool_name == "Bash"
    assert s.session_id == "session"
    assert s.duration_ms == 1000
    assert s.success is True
    assert s.exit_code is None  # no "Exit code:" annotation
    assert s.output_size == len("hello")
    assert s.turn_index == 0


def test_error_span(tmp_path: Path) -> None:
    records = [
        _cc_assistant("Bash", "tid1", "false", "2026-04-21T10:00:00+00:00"),
        _cc_result("tid1", "", "2026-04-21T10:00:00+00:00", is_error=True),
    ]
    p = _write_jsonl(tmp_path, records)
    spans = extract_spans_from_cc_jsonl(p)

    assert len(spans) == 1
    assert spans[0].success is False
    assert spans[0].duration_ms == 0


def test_exit_code_extracted(tmp_path: Path) -> None:
    output = "some output\nExit code: 2\n"
    records = [
        _cc_assistant("Bash", "tid1", "exit 2", "2026-04-21T10:00:00+00:00"),
        _cc_result("tid1", output, "2026-04-21T10:00:00.5+00:00"),
    ]
    p = _write_jsonl(tmp_path, records)
    spans = extract_spans_from_cc_jsonl(p)

    assert spans[0].exit_code == 2


def test_exit_code_not_set_for_non_bash(tmp_path: Path) -> None:
    records = [
        _cc_assistant("Read", "tid1", "/path/to/file", "2026-04-21T10:00:00+00:00"),
        _cc_result("tid1", "Exit code: 0\ncontent", "2026-04-21T10:00:01+00:00"),
    ]
    p = _write_jsonl(tmp_path, records)
    spans = extract_spans_from_cc_jsonl(p)

    assert spans[0].exit_code is None  # exit code only extracted for Bash


def test_multiple_spans_turn_index(tmp_path: Path) -> None:
    records = [
        _cc_assistant("Bash", "tid1", "git status", "2026-04-21T10:00:00+00:00"),
        _cc_result("tid1", "clean", "2026-04-21T10:00:01+00:00"),
        _cc_assistant("Edit", "tid2", "file.py", "2026-04-21T10:00:02+00:00"),
        _cc_result("tid2", "ok", "2026-04-21T10:00:03+00:00"),
    ]
    p = _write_jsonl(tmp_path, records)
    spans = extract_spans_from_cc_jsonl(p)

    assert len(spans) == 2
    assert spans[0].turn_index == 0
    assert spans[1].turn_index == 1


def test_batched_tool_calls(tmp_path: Path) -> None:
    """Multiple tool_use items in a single assistant message share turn_index."""
    records = [
        {
            "type": "assistant",
            "timestamp": "2026-04-21T10:00:00+00:00",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tid1",
                        "name": "Read",
                        "input": {"file_path": "a.py"},
                    },
                    {
                        "type": "tool_use",
                        "id": "tid2",
                        "name": "Read",
                        "input": {"file_path": "b.py"},
                    },
                ]
            },
        },
        _cc_result("tid1", "content a", "2026-04-21T10:00:01+00:00"),
        _cc_result("tid2", "content b", "2026-04-21T10:00:02+00:00"),
    ]
    p = _write_jsonl(tmp_path, records)
    spans = extract_spans_from_cc_jsonl(p)

    assert len(spans) == 2
    assert spans[0].turn_index == spans[1].turn_index == 0


def test_timestamp_is_dispatch_time(tmp_path: Path) -> None:
    """span.timestamp must reflect dispatch time, not result-arrival time."""
    dispatch_ts = "2026-04-21T10:00:00+00:00"
    arrival_ts = "2026-04-21T10:00:05+00:00"
    records = [
        _cc_assistant("Bash", "tid1", "sleep 5", dispatch_ts),
        _cc_result("tid1", "done", arrival_ts),
    ]
    p = _write_jsonl(tmp_path, records)
    spans = extract_spans_from_cc_jsonl(p)

    assert len(spans) == 1
    assert spans[0].timestamp == dispatch_ts
    assert spans[0].duration_ms == 5000


def test_session_id_from_filename(tmp_path: Path) -> None:
    records = [
        _cc_assistant("Bash", "tid1", "echo hi", "2026-04-21T10:00:00+00:00"),
        _cc_result("tid1", "hi", "2026-04-21T10:00:01+00:00"),
    ]
    p = _write_jsonl(tmp_path, records, name="abc123def.jsonl")
    spans = extract_spans_from_cc_jsonl(p)

    assert spans[0].session_id == "abc123def"


def test_session_id_override(tmp_path: Path) -> None:
    records = [
        _cc_assistant("Bash", "tid1", "echo hi", "2026-04-21T10:00:00+00:00"),
        _cc_result("tid1", "hi", "2026-04-21T10:00:01+00:00"),
    ]
    p = _write_jsonl(tmp_path, records)
    spans = extract_spans_from_cc_jsonl(p, session_id="custom-id")

    assert spans[0].session_id == "custom-id"


def test_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    spans = extract_spans_from_cc_jsonl(p)
    assert spans == []


def test_missing_file() -> None:
    spans = extract_spans_from_cc_jsonl(Path("/nonexistent/path/session.jsonl"))
    assert spans == []


def test_malformed_lines(tmp_path: Path) -> None:
    p = tmp_path / "bad.jsonl"
    p.write_text("not json\n{}\n")
    spans = extract_spans_from_cc_jsonl(p)
    assert spans == []


def test_unknown_result_id_ignored(tmp_path: Path) -> None:
    """Result with unmatched tool_use_id should not produce a span."""
    records = [
        _cc_result("unknown-id", "output", "2026-04-21T10:00:00+00:00"),
    ]
    p = _write_jsonl(tmp_path, records)
    spans = extract_spans_from_cc_jsonl(p)
    assert spans == []


# ── SpanAggregates ────────────────────────────────────────────────────────────


def _span(tool: str, dur_ms: int = 100, success: bool = True) -> ToolSpan:
    return ToolSpan(
        span_id="test",
        session_id="s",
        tool_name=tool,
        timestamp="",
        duration_ms=dur_ms,
        success=success,
        input_size=10,
        output_size=20,
        exit_code=None,
        turn_index=0,
    )


def test_aggregates_empty() -> None:
    agg = SpanAggregates.from_spans([])
    assert agg.total_spans == 0
    assert agg.error_rate == 0.0
    assert agg.dominant_tool is None
    assert agg.avg_duration_ms == -1.0
    assert agg.max_duration_ms == -1
    assert agg.retry_depth == 0


def test_aggregates_basic() -> None:
    spans = [_span("Bash"), _span("Edit"), _span("Bash", success=False)]
    agg = SpanAggregates.from_spans(spans)

    assert agg.total_spans == 3
    assert agg.error_spans == 1
    assert abs(agg.error_rate - 1 / 3) < 1e-9
    assert agg.dominant_tool == "Bash"
    assert agg.tool_counts == {"Bash": 2, "Edit": 1}


def test_aggregates_duration() -> None:
    spans = [_span("Bash", 200), _span("Bash", 400), _span("Read", 600)]
    agg = SpanAggregates.from_spans(spans)

    assert agg.avg_duration_ms == pytest.approx(400.0)
    assert agg.max_duration_ms == 600


def test_aggregates_unknown_duration_excluded() -> None:
    spans = [_span("Bash", -1), _span("Edit", 300)]
    agg = SpanAggregates.from_spans(spans)

    assert agg.avg_duration_ms == pytest.approx(300.0)
    assert agg.max_duration_ms == 300


def test_mixed_timezone_timestamps_no_crash(tmp_path: Path) -> None:
    """Mixed tz-aware/naive timestamps should produce dur_ms=-1, not TypeError."""
    records = [
        _cc_assistant("Bash", "tid1", "cmd", "2026-04-21T10:00:00+00:00"),  # tz-aware
        _cc_result("tid1", "done", "2026-04-21T10:00:01"),  # tz-naive
    ]
    p = _write_jsonl(tmp_path, records)
    spans = extract_spans_from_cc_jsonl(p)

    assert len(spans) == 1
    assert spans[0].duration_ms == -1  # sentinel; subtraction failed gracefully


def test_output_size_consistent_with_text_extraction(tmp_path: Path) -> None:
    """output_size should be non-zero when the result is a dict without 'text' key."""
    records = [
        _cc_assistant("Bash", "tid1", "cmd", "2026-04-21T10:00:00+00:00"),
        {
            "type": "user",
            "timestamp": "2026-04-21T10:00:01+00:00",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tid1",
                        "content": [{"type": "image", "source": "data:..."}],
                    }
                ]
            },
        },
    ]
    p = _write_jsonl(tmp_path, records)
    spans = extract_spans_from_cc_jsonl(p)

    assert len(spans) == 1
    # With the fix, output_size reflects the actual str() representation;
    # without it, output_size would be 0 (c.get("text", "") fallback).
    assert spans[0].output_size > 0


def test_aggregates_retry_depth() -> None:
    # Bash × 3 consecutive → 2 retries beyond the first call
    spans = [_span("Bash"), _span("Bash"), _span("Bash"), _span("Edit")]
    agg = SpanAggregates.from_spans(spans)
    assert agg.retry_depth == 2


def test_aggregates_no_retry() -> None:
    spans = [_span("Bash"), _span("Edit"), _span("Read")]
    agg = SpanAggregates.from_spans(spans)
    assert agg.retry_depth == 0  # no consecutive same-tool calls


# ── extract_spans_from_gptme_jsonl ────────────────────────────────────────────


def _gptme_assistant(tool: str, call_id: str, args: dict, ts: str) -> dict:
    args_str = json.dumps(args)
    return {
        "role": "assistant",
        "timestamp": ts,
        "content": f"\n@{tool}(call-{call_id}): {args_str}",
    }


def _gptme_result(content: str, ts: str, pinned: bool = False) -> dict:
    return {"role": "system", "timestamp": ts, "content": content, "pinned": pinned}


def _write_gptme_session(tmp_path: Path, records: list[dict], session_name: str) -> Path:
    """gptme stores conversations as <dir>/conversation.jsonl."""
    session_dir = tmp_path / session_name
    session_dir.mkdir()
    p = session_dir / "conversation.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


def test_gptme_single_shell_span(tmp_path: Path) -> None:
    records = [
        _gptme_assistant("shell", "abc-0", {"command": "echo hi"}, "2026-04-21T10:00:00"),
        _gptme_result(
            "Ran allowlisted command: `echo hi`\n\n```stdout\nhi\n```",
            "2026-04-21T10:00:01",
        ),
    ]
    p = _write_gptme_session(tmp_path, records, "autonomous-beef")
    spans = extract_spans_from_gptme_jsonl(p)

    assert len(spans) == 1
    s = spans[0]
    assert s.tool_name == "shell"
    assert s.session_id == "autonomous-beef"  # directory name
    assert s.duration_ms == 1000
    assert s.success is True
    assert s.exit_code is None
    assert s.turn_index == 0
    assert s.output_size > 0


def test_gptme_error_result_detected(tmp_path: Path) -> None:
    records = [
        _gptme_assistant("gh", "abc-0", {"url": "bad"}, "2026-04-21T10:00:00"),
        _gptme_result("Error: Unknown gh command.", "2026-04-21T10:00:00.1"),
    ]
    p = _write_gptme_session(tmp_path, records, "sess")
    spans = extract_spans_from_gptme_jsonl(p)

    assert len(spans) == 1
    assert spans[0].success is False


def test_gptme_shell_exit_code_overrides_text(tmp_path: Path) -> None:
    """Nonzero exit code must mark success=False even without 'Error:' prefix."""
    records = [
        _gptme_assistant("shell", "abc-0", {"command": "false"}, "2026-04-21T10:00:00"),
        _gptme_result(
            "Ran command: `false`\n\n```stderr\n\n```\nExit code: 1\n",
            "2026-04-21T10:00:00.5",
        ),
    ]
    p = _write_gptme_session(tmp_path, records, "sess")
    spans = extract_spans_from_gptme_jsonl(p)

    assert spans[0].exit_code == 1
    assert spans[0].success is False


def test_gptme_noise_skipped_lessons_warnings(tmp_path: Path) -> None:
    """Lesson injections and system warnings must not be paired as tool results."""
    records = [
        _gptme_assistant("shell", "abc-0", {"command": "ls"}, "2026-04-21T10:00:00"),
        _gptme_result(
            "<system_warning>Token usage: 1/1000</system_warning>", "2026-04-21T10:00:00.1"
        ),
        _gptme_result("# Relevant Lessons\n\n## Some Lesson\n...", "2026-04-21T10:00:00.2"),
        _gptme_result(
            "Shellcheck found potential issues:\n...",
            "2026-04-21T10:00:00.3",
        ),
        _gptme_result("Ran command: `ls`\n\nfile.txt\n", "2026-04-21T10:00:01"),
    ]
    p = _write_gptme_session(tmp_path, records, "sess")
    spans = extract_spans_from_gptme_jsonl(p)

    assert len(spans) == 1
    # Duration must span across the noise messages to the real result
    assert spans[0].duration_ms == 1000
    # Output size should reflect the actual result, not the warnings
    assert "Ran command" in "Ran command: `ls`"
    assert spans[0].output_size == len("Ran command: `ls`\n\nfile.txt\n")


def test_gptme_pinned_system_skipped(tmp_path: Path) -> None:
    """Pinned system messages (system prompt, context) must not pair as results."""
    records = [
        _gptme_result("You are Bob, ...", "2026-04-21T09:00:00", pinned=True),
        _gptme_assistant("shell", "abc-0", {"command": "ls"}, "2026-04-21T10:00:00"),
        _gptme_result("Ran command: `ls`\n\nfile.txt\n", "2026-04-21T10:00:01"),
    ]
    p = _write_gptme_session(tmp_path, records, "sess")
    spans = extract_spans_from_gptme_jsonl(p)

    assert len(spans) == 1


def test_gptme_multiple_tools_fifo_pairing(tmp_path: Path) -> None:
    """Sequential tool calls pair in FIFO order with their results."""
    records = [
        _gptme_assistant("shell", "aa-0", {"command": "a"}, "2026-04-21T10:00:00"),
        _gptme_result("Ran command: `a`\nout-a", "2026-04-21T10:00:01"),
        _gptme_assistant("shell", "bb-1", {"command": "b"}, "2026-04-21T10:00:02"),
        _gptme_result("Ran command: `b`\nout-b", "2026-04-21T10:00:04"),
        _gptme_assistant("save", "cc-2", {"path": "/x", "content": "y"}, "2026-04-21T10:00:05"),
        _gptme_result("Saved to /x", "2026-04-21T10:00:05.1"),
    ]
    p = _write_gptme_session(tmp_path, records, "sess")
    spans = extract_spans_from_gptme_jsonl(p)

    assert len(spans) == 3
    assert [s.tool_name for s in spans] == ["shell", "shell", "save"]
    assert [s.turn_index for s in spans] == [0, 1, 2]
    assert spans[0].duration_ms == 1000
    assert spans[1].duration_ms == 2000
    # save tool completes quickly
    assert 0 <= spans[2].duration_ms <= 1000


def test_gptme_unpaired_dispatch_drops_span(tmp_path: Path) -> None:
    """A dispatch without a following result produces no span."""
    records = [
        _gptme_assistant("shell", "aa-0", {"command": "a"}, "2026-04-21T10:00:00"),
        # No result; conversation cut off
    ]
    p = _write_gptme_session(tmp_path, records, "sess")
    spans = extract_spans_from_gptme_jsonl(p)
    assert spans == []


def test_gptme_empty_args(tmp_path: Path) -> None:
    """Tools called with empty args (e.g. @todo, @complete) work fine."""
    records = [
        {
            "role": "assistant",
            "timestamp": "2026-04-21T10:00:00",
            "content": "\n@todo(call-abc-4): {}",
        },
        _gptme_result("📝 Todo list is empty", "2026-04-21T10:00:00.1"),
    ]
    p = _write_gptme_session(tmp_path, records, "sess")
    spans = extract_spans_from_gptme_jsonl(p)

    assert len(spans) == 1
    assert spans[0].tool_name == "todo"
    assert spans[0].input_size == 0


def test_gptme_session_id_override(tmp_path: Path) -> None:
    records = [
        _gptme_assistant("shell", "aa-0", {"command": "ls"}, "2026-04-21T10:00:00"),
        _gptme_result("Ran command: `ls`\n", "2026-04-21T10:00:01"),
    ]
    p = _write_gptme_session(tmp_path, records, "sess-dir")
    spans = extract_spans_from_gptme_jsonl(p, session_id="explicit-id")
    assert spans[0].session_id == "explicit-id"


def test_gptme_empty_file(tmp_path: Path) -> None:
    session_dir = tmp_path / "empty-sess"
    session_dir.mkdir()
    p = session_dir / "conversation.jsonl"
    p.write_text("")
    spans = extract_spans_from_gptme_jsonl(p)
    assert spans == []


def test_gptme_missing_file() -> None:
    spans = extract_spans_from_gptme_jsonl(Path("/nonexistent/sess/conversation.jsonl"))
    assert spans == []


def test_gptme_malformed_lines_skipped(tmp_path: Path) -> None:
    session_dir = tmp_path / "malformed"
    session_dir.mkdir()
    p = session_dir / "conversation.jsonl"
    p.write_text("not json\n{}\n")
    spans = extract_spans_from_gptme_jsonl(p)
    assert spans == []
