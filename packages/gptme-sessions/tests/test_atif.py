"""Tests for ATIF v1.7 export adapter (gptme_sessions.atif)."""

from __future__ import annotations

from gptme_sessions.atif import session_transcript_to_atif, transcript_to_atif_json
from gptme_sessions.transcript import NormalizedMessage, SessionTranscript

TRANSCRIPT_SCHEMA_VERSION = 1


def _make_transcript(messages: list[NormalizedMessage], **kwargs) -> SessionTranscript:
    defaults = {
        "schema_version": TRANSCRIPT_SCHEMA_VERSION,
        "session_id": "test-session",
        "harness": "gptme",
        "trajectory_path": "/dev/null",
        "model": "claude-sonnet-4-6",
    }
    defaults.update(kwargs)
    return SessionTranscript(messages=messages, **defaults)


# ---------------------------------------------------------------------------
# Envelope shape
# ---------------------------------------------------------------------------


def test_envelope_keys():
    t = _make_transcript([NormalizedMessage(role="user", content="hi")])
    atif = session_transcript_to_atif(t)
    assert atif["schema_version"] == "ATIF-v1.7"
    assert atif["session_id"] == "test-session"
    assert atif["agent"]["name"] == "gptme"
    assert atif["agent"]["version"] == "claude-sonnet-4-6"
    assert isinstance(atif["steps"], list)
    assert len(atif["steps"]) == 1


def test_notes_field():
    t = _make_transcript([NormalizedMessage(role="user", content="hi")])
    atif = session_transcript_to_atif(t, notes="test note")
    assert atif["notes"] == "test note"


def test_no_notes_when_omitted():
    t = _make_transcript([NormalizedMessage(role="user", content="hi")])
    atif = session_transcript_to_atif(t)
    assert "notes" not in atif


def test_extra_agent_fields():
    t = _make_transcript([NormalizedMessage(role="user", content="hi")])
    atif = session_transcript_to_atif(t, extra_agent_fields={"tool_format": "native"})
    assert atif["agent"]["tool_format"] == "native"


def test_json_serializable():
    t = _make_transcript([NormalizedMessage(role="user", content="hi")])
    json_str = transcript_to_atif_json(t)
    assert '"ATIF-v1.7"' in json_str


# ---------------------------------------------------------------------------
# Step source mapping
# ---------------------------------------------------------------------------


def test_user_maps_to_user_source():
    t = _make_transcript([NormalizedMessage(role="user", content="hello")])
    atif = session_transcript_to_atif(t)
    assert atif["steps"][0]["source"] == "user"
    assert atif["steps"][0]["message"] == "hello"


def test_system_maps_to_system_source():
    t = _make_transcript([NormalizedMessage(role="system", content="constraints")])
    atif = session_transcript_to_atif(t)
    assert atif["steps"][0]["source"] == "system"


def test_assistant_maps_to_agent_source():
    t = _make_transcript([NormalizedMessage(role="assistant", content="done")])
    atif = session_transcript_to_atif(t)
    assert atif["steps"][0]["source"] == "agent"


def test_step_ids_are_sequential():
    t = _make_transcript(
        [
            NormalizedMessage(role="user", content="go"),
            NormalizedMessage(role="assistant", content="ok"),
            NormalizedMessage(role="system", content="info"),
        ]
    )
    atif = session_transcript_to_atif(t)
    assert [s["step_id"] for s in atif["steps"]] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Tool call grouping
# ---------------------------------------------------------------------------


def test_tool_call_emits_agent_step_with_tool_calls():
    t = _make_transcript(
        [
            NormalizedMessage(
                role="assistant",
                content="",
                tool_name="Bash",
                tool_input={"command": "ls"},
                tool_call_id="call-1",
            )
        ]
    )
    atif = session_transcript_to_atif(t)
    assert len(atif["steps"]) == 1
    step = atif["steps"][0]
    assert step["source"] == "agent"
    assert step["tool_calls"] == [
        {"tool_call_id": "call-1", "function_name": "Bash", "arguments": {"command": "ls"}}
    ]


def test_text_then_tool_call_merge_into_one_step():
    """Text message immediately followed by a tool call merges into one step."""
    t = _make_transcript(
        [
            NormalizedMessage(role="assistant", content="I'll run ls"),
            NormalizedMessage(
                role="assistant",
                content="",
                tool_name="Bash",
                tool_input={"command": "ls"},
                tool_call_id="call-1",
            ),
        ]
    )
    atif = session_transcript_to_atif(t)
    assert len(atif["steps"]) == 1
    step = atif["steps"][0]
    assert step["message"] == "I'll run ls"
    assert len(step["tool_calls"]) == 1
    assert step["tool_calls"][0]["tool_call_id"] == "call-1"


def test_multiple_tool_calls_in_one_turn():
    t = _make_transcript(
        [
            NormalizedMessage(
                role="assistant",
                content="",
                tool_name="Read",
                tool_input={"file_path": "a.py"},
                tool_call_id="call-a",
            ),
            NormalizedMessage(
                role="assistant",
                content="",
                tool_name="Read",
                tool_input={"file_path": "b.py"},
                tool_call_id="call-b",
            ),
        ]
    )
    atif = session_transcript_to_atif(t)
    assert len(atif["steps"]) == 1
    step = atif["steps"][0]
    assert [tc["tool_call_id"] for tc in step["tool_calls"]] == ["call-a", "call-b"]


def test_tool_call_after_user_starts_new_step():
    """A tool call following a user message must be a new step."""
    t = _make_transcript(
        [
            NormalizedMessage(role="user", content="run ls"),
            NormalizedMessage(
                role="assistant",
                content="",
                tool_name="Bash",
                tool_input={"command": "ls"},
                tool_call_id="call-1",
            ),
        ]
    )
    atif = session_transcript_to_atif(t)
    assert len(atif["steps"]) == 2
    assert atif["steps"][0]["source"] == "user"
    assert atif["steps"][1]["source"] == "agent"


# ---------------------------------------------------------------------------
# Tool results
# ---------------------------------------------------------------------------


def test_tool_result_links_by_call_id():
    t = _make_transcript(
        [
            NormalizedMessage(
                role="assistant",
                content="",
                tool_name="Bash",
                tool_input={"command": "ls"},
                tool_call_id="call-1",
            ),
            NormalizedMessage(
                role="tool_result",
                content="file.py",
                tool_result="file.py",
                tool_call_id="call-1",
            ),
        ]
    )
    atif = session_transcript_to_atif(t)
    assert len(atif["steps"]) == 1
    obs = atif["steps"][0]["observation"]["results"]
    assert len(obs) == 1
    assert obs[0]["source_call_id"] == "call-1"
    assert obs[0]["content"] == "file.py"


def test_unlinked_result_attaches_to_last_agent_step():
    t = _make_transcript(
        [
            NormalizedMessage(role="assistant", content="done"),
            NormalizedMessage(
                role="tool_result",
                content="output",
                tool_result="output",
                tool_call_id=None,
            ),
        ]
    )
    atif = session_transcript_to_atif(t)
    assert len(atif["steps"]) == 1
    obs = atif["steps"][0]["observation"]["results"]
    assert obs[0]["content"] == "output"
    assert "source_call_id" not in obs[0]


def test_tool_result_without_owner_is_skipped():
    """A tool result with no prior agent step is silently dropped (no crash)."""
    t = _make_transcript(
        [
            NormalizedMessage(role="user", content="hi"),
            NormalizedMessage(
                role="tool_result",
                content="orphan",
                tool_result="orphan",
                tool_call_id=None,
            ),
        ]
    )
    atif = session_transcript_to_atif(t)
    # Only the user step survives; the orphan result is dropped.
    assert len(atif["steps"]) == 1
    assert atif["steps"][0]["source"] == "user"


def test_error_tool_result_sets_extra_flag():
    t = _make_transcript(
        [
            NormalizedMessage(
                role="assistant",
                content="",
                tool_name="Bash",
                tool_input={"command": "bad"},
                tool_call_id="call-1",
            ),
            NormalizedMessage(
                role="tool_result",
                content="error: not found",
                tool_result="error: not found",
                tool_call_id="call-1",
                is_error=True,
            ),
        ]
    )
    atif = session_transcript_to_atif(t)
    obs = atif["steps"][0]["observation"]["results"][0]
    assert obs["extra"]["is_error"] is True


def test_explicit_result_closes_implicit_window():
    """After an explicit call-ID result, a subsequent unlinked result gets no owner."""
    t = _make_transcript(
        [
            NormalizedMessage(
                role="assistant",
                content="",
                tool_name="Bash",
                tool_input={"command": "ls"},
                tool_call_id="call-1",
            ),
            NormalizedMessage(
                role="tool_result",
                content="ok",
                tool_result="ok",
                tool_call_id="call-1",
            ),
            # Orphan — arrives after the explicit result closed the window
            NormalizedMessage(
                role="tool_result",
                content="orphan",
                tool_result="orphan",
                tool_call_id=None,
            ),
        ]
    )
    atif = session_transcript_to_atif(t)
    obs = atif["steps"][0]["observation"]["results"]
    assert len(obs) == 1
    assert obs[0]["content"] == "ok"


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def test_timestamp_propagated():
    t = _make_transcript(
        [NormalizedMessage(role="user", content="hi", timestamp="2026-01-01T00:00:00Z")]
    )
    atif = session_transcript_to_atif(t)
    assert atif["steps"][0]["timestamp"] == "2026-01-01T00:00:00Z"


def test_no_timestamp_key_when_absent():
    t = _make_transcript([NormalizedMessage(role="user", content="hi", timestamp=None)])
    atif = session_transcript_to_atif(t)
    assert "timestamp" not in atif["steps"][0]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_messages_gives_empty_steps():
    t = _make_transcript([])
    atif = session_transcript_to_atif(t)
    assert atif["steps"] == []


def test_synthetic_call_id_when_no_tool_call_id():
    """A tool call with no tool_call_id gets a synthetic identifier."""
    t = _make_transcript(
        [
            NormalizedMessage(
                role="assistant",
                content="",
                tool_name="Read",
                tool_input={"file_path": "x.py"},
                tool_call_id=None,
            ),
            NormalizedMessage(
                role="tool_result",
                content="content",
                tool_result="content",
                tool_call_id=None,
            ),
        ]
    )
    atif = session_transcript_to_atif(t)
    # Tool call should have been generated with a synthetic ID
    tc = atif["steps"][0]["tool_calls"][0]
    assert tc["tool_call_id"].startswith("norm-")
    # Result attaches implicitly (no call_id)
    obs = atif["steps"][0]["observation"]["results"]
    assert obs[0]["content"] == "content"


def test_reasoning_message_sets_extra_flag():
    t = _make_transcript(
        [NormalizedMessage(role="assistant", content="thinking...", is_reasoning=True)]
    )
    atif = session_transcript_to_atif(t)
    assert atif["steps"][0].get("extra", {}).get("is_reasoning") is True


def test_no_model_in_agent_when_transcript_has_none():
    t = _make_transcript([NormalizedMessage(role="user", content="hi")], model=None)
    atif = session_transcript_to_atif(t)
    assert "version" not in atif["agent"]


def test_second_text_message_after_tool_calls_is_new_step():
    """A text-only agent message after tool calls (e.g. follow-up) is a new step."""
    t = _make_transcript(
        [
            NormalizedMessage(
                role="assistant",
                content="",
                tool_name="Bash",
                tool_input={"command": "ls"},
                tool_call_id="call-1",
            ),
            NormalizedMessage(role="assistant", content="All done"),
        ]
    )
    atif = session_transcript_to_atif(t)
    assert len(atif["steps"]) == 2
    assert atif["steps"][0]["tool_calls"][0]["tool_call_id"] == "call-1"
    assert atif["steps"][1]["message"] == "All done"
    assert "tool_calls" not in atif["steps"][1]
