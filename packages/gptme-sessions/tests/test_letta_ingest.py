"""Tests for Letta trajectory-v1 ingest (normalize_letta)."""

from __future__ import annotations

from gptme_sessions.letta import session_transcript_to_letta
from gptme_sessions.transcript import (
    TRANSCRIPT_SCHEMA_VERSION,
    NormalizedMessage,
    SessionTranscript,
    normalize_letta,
)


def _make_transcript(messages: list[NormalizedMessage], **kwargs) -> SessionTranscript:
    defaults = {
        "schema_version": TRANSCRIPT_SCHEMA_VERSION,
        "session_id": "test-session",
        "harness": "gptme",
        "trajectory_path": "/dev/null",
        "model": "claude-sonnet-4-6",
        "project": "/home/bob/bob",
    }
    defaults.update(kwargs)
    return SessionTranscript(messages=messages, **defaults)


# ---------------------------------------------------------------------------
# Meta record → SessionTranscript metadata
# ---------------------------------------------------------------------------


def test_meta_fields_populated():
    records = [
        {"role": "meta", "source": "claude-code", "cwd": "/work/project", "model": "gpt-6"},
        {"role": "user", "content": "hello", "timestamp": "2026-01-01T00:00:00.000Z"},
    ]
    t = normalize_letta(records)
    assert t.harness == "claude-code"
    assert t.project == "/work/project"
    assert t.model == "gpt-6"


def test_missing_meta_uses_defaults():
    records = [{"role": "user", "content": "hi", "timestamp": "2026-01-01T00:00:00.000Z"}]
    t = normalize_letta(records)
    assert t.harness == "unknown"
    assert t.project is None
    assert t.model is None


def test_custom_session_id():
    records = [{"role": "meta", "source": "gptme"}]
    t = normalize_letta(records, session_id="my-session")
    assert t.session_id == "my-session"


def test_default_session_id():
    records = [{"role": "meta", "source": "gptme"}]
    t = normalize_letta(records)
    assert t.session_id == "letta-ingest"


def test_timestamps_populated():
    records = [
        {"role": "meta", "source": "gptme"},
        {"role": "user", "content": "a", "timestamp": "2026-01-01T00:00:00.000Z"},
        {"role": "assistant", "content": "b", "timestamp": "2026-01-01T00:01:00.000Z"},
    ]
    t = normalize_letta(records)
    assert t.started_at == "2026-01-01T00:00:00+00:00"
    assert t.last_activity == "2026-01-01T00:01:00+00:00"


def test_timestamp_bounds_use_parsed_extrema_without_reordering_messages():
    records = [
        {"role": "user", "content": "later", "timestamp": "2026-01-01T00:30:00Z"},
        {"role": "assistant", "content": "earlier", "timestamp": "2026-01-01T01:00:00+02:00"},
        {"role": "observation", "content": "last", "timestamp": "2026-01-01T01:00:00"},
        {"role": "user", "content": "invalid", "timestamp": "not-a-timestamp"},
    ]
    transcript = normalize_letta(records)
    assert transcript.started_at == "2026-01-01T01:00:00+02:00"
    assert transcript.last_activity == "2026-01-01T01:00:00+00:00"
    assert [m.content for m in transcript.messages] == ["later", "earlier", "invalid"]
    assert [m.timestamp for m in transcript.messages] == [
        "2026-01-01T00:30:00+00:00",
        "2026-01-01T01:00:00+02:00",
        None,
    ]


def test_capabilities_set_when_messages_present():
    records = [{"role": "user", "content": "hi", "timestamp": "2026-01-01T00:00:00.000Z"}]
    t = normalize_letta(records)
    assert "view_transcript" in t.capabilities


def test_capabilities_empty_when_no_messages():
    records = [{"role": "meta", "source": "gptme"}]
    t = normalize_letta(records)
    assert t.capabilities == []
    assert t.started_at is None
    assert t.last_activity is None


def test_invalid_timestamp_does_not_set_bounds():
    t = normalize_letta([{"role": "user", "content": "hi", "timestamp": "invalid"}])
    assert t.started_at is None
    assert t.last_activity is None
    assert t.messages[0].timestamp is None


# ---------------------------------------------------------------------------
# Role mapping
# ---------------------------------------------------------------------------


def test_user_role():
    records = [{"role": "user", "content": "hello", "timestamp": "2026-01-01T00:00:00.000Z"}]
    t = normalize_letta(records)
    assert len(t.messages) == 1
    assert t.messages[0].role == "user"
    assert t.messages[0].content == "hello"


def test_system_role():
    records = [{"role": "system", "content": "be helpful", "timestamp": "2026-01-01T00:00:00.000Z"}]
    t = normalize_letta(records)
    assert t.messages[0].role == "system"
    assert t.messages[0].content == "be helpful"


def test_plain_assistant():
    records = [{"role": "assistant", "content": "done", "timestamp": "2026-01-01T00:00:00.000Z"}]
    t = normalize_letta(records)
    assert t.messages[0].role == "assistant"
    assert t.messages[0].content == "done"
    assert t.messages[0].tool_name is None


def test_reasoning_role():
    records = [
        {"role": "reasoning", "content": "let me think", "timestamp": "2026-01-01T00:00:00.000Z"}
    ]
    t = normalize_letta(records)
    assert t.messages[0].role == "assistant"
    assert t.messages[0].is_reasoning is True
    assert t.messages[0].content == "let me think"


def test_empty_content_skipped_for_user():
    records = [{"role": "user", "content": "", "timestamp": "2026-01-01T00:00:00.000Z"}]
    t = normalize_letta(records)
    assert len(t.messages) == 0


def test_meta_record_not_emitted_as_message():
    records = [{"role": "meta", "source": "gptme"}]
    t = normalize_letta(records)
    assert len(t.messages) == 0


def test_empty_reasoning_record_skipped():
    records = [{"role": "reasoning", "content": "", "timestamp": "2026-01-01T00:00:00.000Z"}]
    t = normalize_letta(records)
    assert len(t.messages) == 0


def test_empty_tool_record_skipped_but_keeps_timestamp():
    records = [
        {"role": "tool", "timestamp": "2026-01-01T00:00:00.000Z"},
        {"role": "user", "content": "hi", "timestamp": "2026-01-01T00:00:05.000Z"},
    ]
    t = normalize_letta(records)
    assert [m.role for m in t.messages] == ["user"]
    assert t.started_at == "2026-01-01T00:00:00+00:00"
    assert t.last_activity == "2026-01-01T00:00:05+00:00"


# ---------------------------------------------------------------------------
# Tool call mapping
# ---------------------------------------------------------------------------


def test_tool_call_fields():
    records = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call-1", "name": "bash", "args": '{"cmd": "ls"}'}],
            "timestamp": "2026-01-01T00:00:00.000Z",
        }
    ]
    t = normalize_letta(records)
    msg = t.messages[0]
    assert msg.role == "assistant"
    assert msg.tool_name == "bash"
    assert msg.tool_input == {"cmd": "ls"}
    assert msg.tool_call_id == "call-1"


def test_multiple_tool_calls_keep_all_calls_and_result_links():
    records = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "a", "name": "bash", "args": '{"cmd": "ls"}'},
                {"id": "b", "name": "read", "args": '{"path": "/foo"}'},
            ],
            "timestamp": "2026-01-01T00:00:00Z",
        },
        {"role": "tool", "tool_call_id": "a", "content": "foo"},
        {"role": "tool", "tool_call_id": "b", "content": "contents"},
    ]
    transcript = normalize_letta(records)
    calls = [m for m in transcript.messages if m.role == "assistant"]
    results = [m for m in transcript.messages if m.role == "tool_result"]
    assert [m.tool_call_id for m in calls] == ["a", "b"]
    assert [m.tool_name for m in calls] == ["bash", "read"]
    assert [m.tool_input for m in calls] == [{"cmd": "ls"}, {"path": "/foo"}]
    assert [m.timestamp for m in calls] == ["2026-01-01T00:00:00+00:00"] * 2
    assert [m.tool_call_id for m in results] == [m.tool_call_id for m in calls]


def test_tool_call_args_dict_passthrough():
    records = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "name": "read", "args": {"path": "/foo"}}],
            "timestamp": "2026-01-01T00:00:00.000Z",
        }
    ]
    t = normalize_letta(records)
    assert t.messages[0].tool_input == {"path": "/foo"}


def test_tool_call_malformed_args():
    records = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "name": "x", "args": "not valid json {{"}],
            "timestamp": "2026-01-01T00:00:00.000Z",
        }
    ]
    t = normalize_letta(records)
    assert t.messages[0].tool_input == {"_raw": "not valid json {{"}


def test_tool_result_ok():
    records = [
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "output here",
            "ok": True,
            "timestamp": "2026-01-01T00:00:00.000Z",
        }
    ]
    t = normalize_letta(records)
    msg = t.messages[0]
    assert msg.role == "tool_result"
    assert msg.tool_result == "output here"
    assert msg.tool_call_id == "call-1"
    assert msg.is_error is False


def test_tool_result_not_ok():
    records = [
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "error!",
            "ok": False,
            "timestamp": "2026-01-01T00:00:00.000Z",
        }
    ]
    t = normalize_letta(records)
    assert t.messages[0].is_error is True


def test_tool_result_with_id_but_no_content_kept():
    records = [
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "",
            "timestamp": "2026-01-01T00:00:00.000Z",
        }
    ]
    t = normalize_letta(records)
    assert len(t.messages) == 1
    assert t.messages[0].tool_call_id == "c1"
    assert t.messages[0].content == ""


def test_tool_result_missing_ok_defaults_false():
    records = [
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "result",
            "timestamp": "2026-01-01T00:00:00.000Z",
        }
    ]
    t = normalize_letta(records)
    assert t.messages[0].is_error is False


# ---------------------------------------------------------------------------
# Round-trip: SessionTranscript → Letta → normalize_letta → SessionTranscript
# ---------------------------------------------------------------------------


def test_round_trip_user_assistant():
    original = _make_transcript(
        [
            NormalizedMessage(role="user", content="hello", timestamp="2026-01-01T00:00:00Z"),
            NormalizedMessage(
                role="assistant", content="hi there", timestamp="2026-01-01T00:00:01Z"
            ),
        ]
    )
    records, diagnostics = session_transcript_to_letta(original)
    recovered = normalize_letta(records, session_id=original.session_id)
    assert recovered.schema_version == original.schema_version == TRANSCRIPT_SCHEMA_VERSION
    assert recovered.harness == original.harness
    assert recovered.model == original.model
    assert recovered.project == original.project
    user_msgs = [m for m in recovered.messages if m.role == "user"]
    asst_msgs = [m for m in recovered.messages if m.role == "assistant" and not m.is_reasoning]
    assert len(user_msgs) == 1
    assert user_msgs[0].content == "hello"
    assert len(asst_msgs) == 1
    assert asst_msgs[0].content == "hi there"


def test_round_trip_tool_call():
    original = _make_transcript(
        [
            NormalizedMessage(role="user", content="run ls", timestamp="2026-01-01T00:00:00Z"),
            NormalizedMessage(
                role="assistant",
                content="",
                tool_name="bash",
                tool_input={"cmd": "ls -la"},
                tool_call_id="abc123",
                timestamp="2026-01-01T00:00:01Z",
            ),
            NormalizedMessage(
                role="tool_result",
                content="file.txt",
                tool_result="file.txt",
                tool_call_id="abc123",
                is_error=False,
                timestamp="2026-01-01T00:00:02Z",
            ),
        ]
    )
    records, _ = session_transcript_to_letta(original)
    recovered = normalize_letta(records, session_id=original.session_id)
    tool_calls = [m for m in recovered.messages if m.tool_name]
    tool_results = [m for m in recovered.messages if m.role == "tool_result"]
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "bash"
    assert tool_calls[0].tool_input == {"cmd": "ls -la"}
    assert tool_calls[0].tool_call_id == "abc123"
    assert len(tool_results) == 1
    assert tool_results[0].tool_result == "file.txt"
    assert tool_results[0].tool_call_id == "abc123"
    assert tool_results[0].is_error is False


def test_round_trip_reasoning():
    original = _make_transcript(
        [
            NormalizedMessage(
                role="assistant",
                content="thinking step",
                is_reasoning=True,
                timestamp="2026-01-01T00:00:00Z",
            ),
        ]
    )
    records, _ = session_transcript_to_letta(original)
    recovered = normalize_letta(records)
    reasoning = [m for m in recovered.messages if m.is_reasoning]
    assert len(reasoning) == 1
    assert reasoning[0].content == "thinking step"


def test_observation_records_are_skipped_but_keep_timestamps():
    """Schema-valid ``observation`` records have no NormalizedMessage equivalent.

    They must not be mapped onto ``user`` (that would report environment
    feedback as human input), but their timestamps still bound the transcript.
    """
    records = [
        {"role": "meta", "source": "letta"},
        {"role": "user", "content": "go", "timestamp": "2026-01-01T00:00:00.000Z"},
        {
            "role": "observation",
            "content": "sandbox is warm",
            "timestamp": "2026-01-01T00:05:00.000Z",
        },
    ]
    transcript = normalize_letta(records)
    assert [m.role for m in transcript.messages] == ["user"]
    assert transcript.started_at == "2026-01-01T00:00:00+00:00"
    assert transcript.last_activity == "2026-01-01T00:05:00+00:00"
