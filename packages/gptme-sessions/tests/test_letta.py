"""Tests for Letta trajectory-v1 export adapter (gptme_sessions.letta)."""

from __future__ import annotations

import json

import pytest

from gptme_sessions.letta import session_transcript_to_letta, transcript_to_letta_json
from gptme_sessions.transcript import NormalizedMessage, SessionTranscript

TRANSCRIPT_SCHEMA_VERSION = 1
_TS = "2026-10-18T10:00:00.000Z"


def _make_transcript(messages: list[NormalizedMessage], **kwargs) -> SessionTranscript:
    defaults = {
        "schema_version": TRANSCRIPT_SCHEMA_VERSION,
        "session_id": "test-session",
        "harness": "gptme",
        "trajectory_path": "/dev/null",
        "model": "claude-sonnet-4-6",
        "project": "/home/user/project",
    }
    defaults.update(kwargs)
    return SessionTranscript(messages=messages, **defaults)


# ---------------------------------------------------------------------------
# Meta record
# ---------------------------------------------------------------------------


def test_meta_record_is_first():
    t = _make_transcript([NormalizedMessage(role="user", content="hi", timestamp=_TS)])
    records, _ = session_transcript_to_letta(t)
    assert records[0]["role"] == "meta"
    assert records[0]["source"] == "gptme"


def test_meta_carries_model_and_cwd():
    t = _make_transcript([NormalizedMessage(role="user", content="hi", timestamp=_TS)])
    records, _ = session_transcript_to_letta(t)
    meta = records[0]
    assert meta["model"] == "claude-sonnet-4-6"
    assert meta["cwd"] == "/home/user/project"


def test_meta_no_extra_fields():
    """meta record must not carry undeclared fields (additionalProperties: false)."""
    t = _make_transcript([NormalizedMessage(role="user", content="hi", timestamp=_TS)])
    records, _ = session_transcript_to_letta(t)
    meta = records[0]
    allowed = {"role", "source", "cwd", "git_branch", "model"}
    assert set(meta.keys()) <= allowed


# ---------------------------------------------------------------------------
# Role mapping
# ---------------------------------------------------------------------------


def test_user_record():
    t = _make_transcript([NormalizedMessage(role="user", content="hello", timestamp=_TS)])
    records, _ = session_transcript_to_letta(t)
    r = records[1]
    assert r["role"] == "user"
    assert r["content"] == "hello"
    assert r["timestamp"] == _TS


def test_system_record():
    t = _make_transcript([NormalizedMessage(role="system", content="constraints", timestamp=_TS)])
    records, _ = session_transcript_to_letta(t)
    assert records[1]["role"] == "system"
    assert records[1]["content"] == "constraints"


def test_assistant_text_record():
    t = _make_transcript(
        [NormalizedMessage(role="assistant", content="I can help.", timestamp=_TS)]
    )
    records, _ = session_transcript_to_letta(t)
    r = records[1]
    assert r["role"] == "assistant"
    assert r["content"] == "I can help."
    assert "tool_calls" not in r


def test_reasoning_record():
    t = _make_transcript(
        [
            NormalizedMessage(
                role="assistant", content="thinking...", timestamp=_TS, is_reasoning=True
            )
        ]
    )
    records, _ = session_transcript_to_letta(t)
    assert records[1]["role"] == "reasoning"
    assert records[1]["content"] == "thinking..."


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------


def test_tool_call_record():
    t = _make_transcript(
        [
            NormalizedMessage(
                role="assistant",
                content="",
                timestamp=_TS,
                tool_name="bash",
                tool_input={"command": "ls"},
                tool_call_id="call_1",
            )
        ]
    )
    records, _ = session_transcript_to_letta(t)
    r = records[1]
    assert r["role"] == "assistant"
    assert r["content"] is None
    assert r["tool_calls"] == [{"id": "call_1", "name": "bash", "args": '{"command": "ls"}'}]


def test_tool_call_args_is_json_string():
    """Letta requires args as a JSON-encoded string, not a dict."""
    t = _make_transcript(
        [
            NormalizedMessage(
                role="assistant",
                content="",
                timestamp=_TS,
                tool_name="read_file",
                tool_input={"path": "/etc/hosts"},
                tool_call_id="call_2",
            )
        ]
    )
    records, _ = session_transcript_to_letta(t)
    args = records[1]["tool_calls"][0]["args"]
    assert isinstance(args, str)
    assert json.loads(args) == {"path": "/etc/hosts"}


def test_tool_result_record():
    t = _make_transcript(
        [
            NormalizedMessage(
                role="tool_result",
                content="file.txt\ndir/",
                timestamp=_TS,
                tool_result="file.txt\ndir/",
                tool_call_id="call_1",
                is_error=False,
            )
        ]
    )
    records, _ = session_transcript_to_letta(t)
    r = records[1]
    assert r["role"] == "tool"
    assert r["tool_call_id"] == "call_1"
    assert r["content"] == "file.txt\ndir/"
    assert r["ok"] is True


def test_tool_result_error():
    t = _make_transcript(
        [
            NormalizedMessage(
                role="tool_result",
                content="Permission denied",
                timestamp=_TS,
                tool_result="Permission denied",
                tool_call_id="call_err",
                is_error=True,
            )
        ]
    )
    records, _ = session_transcript_to_letta(t)
    r = records[1]
    assert r["ok"] is False


def test_tool_call_result_round_trip():
    """A tool call immediately followed by its result produces linked records."""
    t = _make_transcript(
        [
            NormalizedMessage(
                role="assistant",
                content="",
                timestamp=_TS,
                tool_name="bash",
                tool_input={"command": "pwd"},
                tool_call_id="call_1",
            ),
            NormalizedMessage(
                role="tool_result",
                content="/workspace",
                timestamp=_TS,
                tool_result="/workspace",
                tool_call_id="call_1",
                is_error=False,
            ),
        ]
    )
    records, _ = session_transcript_to_letta(t)
    call = records[1]
    result = records[2]
    assert call["tool_calls"][0]["id"] == "call_1"
    assert result["tool_call_id"] == "call_1"


# ---------------------------------------------------------------------------
# Timestamp handling
# ---------------------------------------------------------------------------


def test_missing_timestamp_diagnostic():
    """Missing timestamps emit a diagnostic and substitute placeholder."""
    t = _make_transcript([NormalizedMessage(role="user", content="hi")])
    records, diagnostics = session_transcript_to_letta(t)
    assert any("substituted placeholder" in d for d in diagnostics)
    assert records[1]["timestamp"] == "1970-01-01T00:00:00.000Z"


def test_drop_missing_timestamp():
    t = _make_transcript([NormalizedMessage(role="user", content="hi")])
    records, diagnostics = session_transcript_to_letta(t, drop_missing_timestamps=True)
    # Only meta record survives
    assert len(records) == 1
    assert any("dropped" in d for d in diagnostics)


# ---------------------------------------------------------------------------
# No metrics
# ---------------------------------------------------------------------------


def test_no_metrics_in_records():
    """Letta format intentionally omits per-step token counts."""
    t = _make_transcript(
        [
            NormalizedMessage(
                role="assistant",
                content="Done.",
                timestamp=_TS,
                usage={"input_tokens": 100, "output_tokens": 50},
            )
        ]
    )
    records, _ = session_transcript_to_letta(t)
    assert "metrics" not in records[1]
    assert "usage" not in records[1]


# ---------------------------------------------------------------------------
# Schema field constraints (additionalProperties: false)
# ---------------------------------------------------------------------------

_ALLOWED_FIELDS = {
    "meta": {"role", "source", "cwd", "git_branch", "model"},
    "user": {"role", "content", "timestamp"},
    "system": {"role", "content", "timestamp"},
    "reasoning": {"role", "content", "timestamp"},
    "assistant": {"role", "content", "timestamp", "tool_calls"},
    "tool": {"role", "tool_call_id", "content", "ok", "timestamp"},
}


@pytest.mark.parametrize(
    "msg",
    [
        NormalizedMessage(role="user", content="hi", timestamp=_TS),
        NormalizedMessage(role="system", content="sys", timestamp=_TS),
        NormalizedMessage(role="assistant", content="text", timestamp=_TS),
        NormalizedMessage(
            role="assistant",
            content="",
            timestamp=_TS,
            tool_name="bash",
            tool_input={},
            tool_call_id="c1",
        ),
        NormalizedMessage(
            role="tool_result",
            content="ok",
            timestamp=_TS,
            tool_result="ok",
            tool_call_id="c1",
            is_error=False,
        ),
        NormalizedMessage(role="assistant", content="think", timestamp=_TS, is_reasoning=True),
    ],
)
def test_no_extra_fields(msg):
    t = _make_transcript([msg])
    records, _ = session_transcript_to_letta(t)
    for r in records[1:]:  # skip meta
        allowed = _ALLOWED_FIELDS[r["role"]]
        assert (
            set(r.keys()) <= allowed
        ), f"Extra fields in {r['role']} record: {set(r.keys()) - allowed}"


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------


def test_json_serializable():
    t = _make_transcript([NormalizedMessage(role="user", content="hi", timestamp=_TS)])
    json_str = transcript_to_letta_json(t)
    parsed = json.loads(json_str)
    assert isinstance(parsed, list)
    assert parsed[0]["role"] == "meta"


def test_json_is_array_not_object():
    """Letta format is a flat array, not a wrapped envelope like ATIF."""
    t = _make_transcript([NormalizedMessage(role="user", content="hi", timestamp=_TS)])
    json_str = transcript_to_letta_json(t)
    assert json_str.strip().startswith("[")


# ---------------------------------------------------------------------------
# Empty content guards
# ---------------------------------------------------------------------------


def test_empty_assistant_text_dropped():
    """Empty assistant text records are invalid (schema: minLength 1). Drop them."""
    t = _make_transcript([NormalizedMessage(role="assistant", content="", timestamp=_TS)])
    records, _ = session_transcript_to_letta(t)
    # Only the meta record
    assert len(records) == 1


def test_empty_user_text_dropped():
    t = _make_transcript([NormalizedMessage(role="user", content="  ", timestamp=_TS)])
    records, _ = session_transcript_to_letta(t)
    assert len(records) == 1
