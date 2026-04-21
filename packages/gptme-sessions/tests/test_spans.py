"""Tests for span-level tracing (gptme_sessions.spans)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gptme_sessions.spans import (
    SpanAggregates,
    ToolSpan,
    extract_spans_from_cc_jsonl,
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
