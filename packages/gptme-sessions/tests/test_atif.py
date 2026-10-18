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
    assert atif["agent"]["model_name"] == "claude-sonnet-4-6"
    assert atif["agent"]["version"] == "unknown"
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


def test_text_embedded_in_tool_call_message_is_preserved():
    """Native formats emit assistant text and the call in one message."""
    t = _make_transcript(
        [
            NormalizedMessage(
                role="assistant",
                content="I'll run ls",
                tool_name="Bash",
                tool_input={"command": "ls"},
                tool_call_id="call-1",
            )
        ]
    )
    atif = session_transcript_to_atif(t)
    assert len(atif["steps"]) == 1
    step = atif["steps"][0]
    assert step["message"] == "I'll run ls"
    assert step["tool_calls"][0]["tool_call_id"] == "call-1"


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


def test_agent_version_is_required_even_without_model():
    """ATIF v1.7 requires agent.version; a missing model must not drop it."""
    t = _make_transcript([NormalizedMessage(role="user", content="hi")], model=None)
    atif = session_transcript_to_atif(t)
    assert atif["agent"]["version"] == "unknown"
    assert "model_name" not in atif["agent"]


def test_extra_agent_fields_override_version():
    t = _make_transcript([NormalizedMessage(role="user", content="hi")])
    atif = session_transcript_to_atif(t, extra_agent_fields={"version": "0.35.1"})
    assert atif["agent"]["version"] == "0.35.1"
    assert atif["agent"]["model_name"] == "claude-sonnet-4-6"


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


def test_unlinked_result_attaches_to_tool_call_step_not_subsequent_text_step():
    """Implicit target must keep pointing to the step with the unlinked tool call
    even after a subsequent text-only agent step is created from the same turn."""
    t = _make_transcript(
        [
            # Step A: tool call with no ID
            NormalizedMessage(
                role="assistant",
                content="",
                tool_name="Read",
                tool_input={"file_path": "x.py"},
                tool_call_id=None,
            ),
            # Step B: text-only message after tool call — flushes A, creates B
            NormalizedMessage(role="assistant", content="All done"),
            # Result for the call in A — must attach to A, not B
            NormalizedMessage(
                role="tool_result",
                content="file content",
                tool_result="file content",
                tool_call_id=None,
            ),
        ]
    )
    atif = session_transcript_to_atif(t)
    assert len(atif["steps"]) == 2
    step_a = atif["steps"][0]
    step_b = atif["steps"][1]
    assert step_a["tool_calls"][0]["tool_call_id"].startswith("norm-")
    # Result must be on step A, not step B
    assert "observation" in step_a
    assert step_a["observation"]["results"][0]["content"] == "file content"
    assert "observation" not in step_b


def test_text_in_tool_call_message_after_preamble_is_kept():
    """A preamble text part plus a text-carrying tool call both survive."""
    t = _make_transcript(
        [
            NormalizedMessage(role="assistant", content="Let me check"),
            NormalizedMessage(
                role="assistant",
                content="Running ls",
                tool_name="Bash",
                tool_input={"command": "ls"},
                tool_call_id="call-1",
            ),
        ]
    )
    atif = session_transcript_to_atif(t)
    assert len(atif["steps"]) == 1
    step = atif["steps"][0]
    assert "Let me check" in step["message"]
    assert "Running ls" in step["message"]
    assert step["tool_calls"][0]["tool_call_id"] == "call-1"


def test_new_tool_call_reopens_implicit_window_after_explicit_result():
    t = _make_transcript(
        [
            NormalizedMessage(role="assistant", content="", tool_name="Read", tool_call_id="a"),
            NormalizedMessage(role="tool_result", content="first", tool_call_id="a"),
            NormalizedMessage(role="assistant", content="", tool_name="Read"),
            NormalizedMessage(role="tool_result", content="second"),
        ]
    )
    step = session_transcript_to_atif(t)["steps"][0]
    assert [r["content"] for r in step["observation"]["results"]] == ["first", "second"]


def test_new_tool_call_refreshes_implicit_target_after_text():
    t = _make_transcript(
        [
            NormalizedMessage(role="assistant", content="", tool_name="Read"),
            NormalizedMessage(role="assistant", content="Next call"),
            NormalizedMessage(role="assistant", content="", tool_name="Bash"),
            NormalizedMessage(role="tool_result", content="second call output"),
        ]
    )
    first, second = session_transcript_to_atif(t)["steps"]
    assert "observation" not in first
    assert second["observation"]["results"][0]["content"] == "second call output"


def test_multiple_text_steps_do_not_steal_unlinked_tool_result():
    t = _make_transcript(
        [
            NormalizedMessage(role="assistant", content="", tool_name="Read"),
            NormalizedMessage(role="assistant", content="First text"),
            NormalizedMessage(role="assistant", content="Second text"),
            NormalizedMessage(role="tool_result", content="call output"),
        ]
    )
    first, *text_steps = session_transcript_to_atif(t)["steps"]
    assert first["observation"]["results"][0]["content"] == "call output"
    assert all("observation" not in step for step in text_steps)


def test_duplicate_call_id_in_same_step_is_rejected():
    import pytest

    t = _make_transcript(
        [
            NormalizedMessage(role="assistant", content="", tool_name="Read", tool_call_id="x"),
            NormalizedMessage(role="assistant", content="", tool_name="Bash", tool_call_id="x"),
        ]
    )
    with pytest.raises(ValueError, match="Duplicate tool_call_id: x"):
        session_transcript_to_atif(t)


def test_duplicate_call_id_across_steps_is_rejected():
    import pytest

    t = _make_transcript(
        [
            NormalizedMessage(role="assistant", content="", tool_name="Read", tool_call_id="x"),
            NormalizedMessage(role="user", content="retry"),
            NormalizedMessage(role="assistant", content="", tool_name="Bash", tool_call_id="x"),
            NormalizedMessage(role="tool_result", content="retry output", tool_call_id="x"),
        ]
    )
    with pytest.raises(ValueError, match="Duplicate tool_call_id: x"):
        session_transcript_to_atif(t)


def test_synthetic_id_does_not_collide_with_later_source_id():
    t = _make_transcript(
        [
            NormalizedMessage(role="assistant", content="", tool_name="Read"),
            NormalizedMessage(
                role="assistant", content="", tool_name="Bash", tool_call_id="norm-1-1"
            ),
            NormalizedMessage(role="tool_result", content="explicit", tool_call_id="norm-1-1"),
        ]
    )
    step = session_transcript_to_atif(t)["steps"][0]
    ids = [call["tool_call_id"] for call in step["tool_calls"]]
    assert len(set(ids)) == 2
    assert ids[1] == "norm-1-1"
    assert step["observation"]["results"][0]["source_call_id"] == ids[1]


# ---------------------------------------------------------------------------
# Metrics from NormalizedMessage.usage
# ---------------------------------------------------------------------------


def test_metrics_from_usage_on_text_step():
    """Per-message usage propagates to the agent step metrics block."""
    t = _make_transcript(
        [
            NormalizedMessage(role="user", content="go"),
            NormalizedMessage(
                role="assistant",
                content="done",
                usage={"input_tokens": 100, "output_tokens": 20},
            ),
        ]
    )
    atif = session_transcript_to_atif(t)
    agent_step = atif["steps"][1]
    assert agent_step["source"] == "agent"
    assert agent_step["metrics"] == {"prompt_tokens": 100, "completion_tokens": 20}


def test_metrics_from_usage_on_tool_call_step():
    """Usage on a tool-call message populates step metrics."""
    t = _make_transcript(
        [
            NormalizedMessage(
                role="assistant",
                content="",
                tool_name="Bash",
                tool_input={"command": "ls"},
                tool_call_id="c1",
                usage={"input_tokens": 50, "output_tokens": 10},
            ),
        ]
    )
    atif = session_transcript_to_atif(t)
    step = atif["steps"][0]
    assert step["metrics"] == {"prompt_tokens": 50, "completion_tokens": 10}


def test_metrics_accumulated_across_merged_tool_calls():
    """Multiple tool calls merged into one step sum their usage."""
    t = _make_transcript(
        [
            NormalizedMessage(
                role="assistant",
                content="",
                tool_name="Read",
                tool_call_id="c1",
                usage={"input_tokens": 100, "output_tokens": 5},
            ),
            NormalizedMessage(
                role="assistant",
                content="",
                tool_name="Bash",
                tool_call_id="c2",
                usage={"input_tokens": 0, "output_tokens": 8},
            ),
        ]
    )
    atif = session_transcript_to_atif(t)
    assert len(atif["steps"]) == 1
    assert atif["steps"][0]["metrics"] == {"prompt_tokens": 100, "completion_tokens": 13}


def test_cache_tokens_fold_into_prompt_tokens():
    """ATIF prompt_tokens is the total including cached; gptme excludes it."""
    t = _make_transcript(
        [
            NormalizedMessage(
                role="assistant",
                content="done",
                usage={
                    "input_tokens": 1499,
                    "output_tokens": 246,
                    "cache_read_tokens": 4224,
                    "cache_creation_tokens": 0,
                },
            )
        ]
    )
    atif = session_transcript_to_atif(t)
    assert atif["steps"][0]["metrics"] == {
        "prompt_tokens": 5723,
        "completion_tokens": 246,
        "cached_tokens": 4224,
    }


def test_metrics_keys_are_atif_names_only():
    """ATIF ``Metrics`` is extra=forbid; harness-native key names invalidate it."""
    allowed = {
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "cost_usd",
        "prompt_token_ids",
        "completion_token_ids",
        "logprobs",
        "extra",
    }
    t = _make_transcript(
        [
            NormalizedMessage(
                role="assistant",
                content="done",
                usage={
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_tokens": 7,
                    "cache_creation_tokens": 1,
                    "reasoning_tokens": 3,
                    "cost": 0.002,
                },
            )
        ]
    )
    atif = session_transcript_to_atif(t)
    metrics = atif["steps"][0]["metrics"]
    assert set(metrics) <= allowed
    assert metrics["cost_usd"] == 0.002


def test_no_metrics_when_usage_absent():
    """Steps without usage data have no metrics key."""
    t = _make_transcript([NormalizedMessage(role="assistant", content="no usage")])
    atif = session_transcript_to_atif(t)
    assert "metrics" not in atif["steps"][0]


# ---------------------------------------------------------------------------
# Integration: real gptme conversation.jsonl -> read_transcript -> ATIF
# ---------------------------------------------------------------------------


def test_real_gptme_jsonl_exports_atif_valid_envelope(tmp_path):
    """The schema bugs shipped because tests built transcripts by hand.

    This goes through the real file boundary (``read_transcript``) so a future
    field-name drift in gptme normalization or the adapter is caught here.
    """
    import json

    from gptme_sessions.transcript import read_transcript

    records = [
        {"role": "system", "content": "You are gptme.", "timestamp": "2026-10-17T23:00:00+00:00"},
        {"role": "user", "content": "write it", "timestamp": "2026-10-17T23:00:01+00:00"},
        {
            "role": "assistant",
            "content": '@save(c1): {"path": "a.py", "content": "print(1)"}',
            "timestamp": "2026-10-17T23:00:02+00:00",
            "metadata": {
                "usage": {
                    "input_tokens": 1499,
                    "output_tokens": 246,
                    "cache_read_tokens": 4224,
                    "cache_creation_tokens": 0,
                }
            },
        },
        {"role": "system", "content": "Saved to a.py", "timestamp": "2026-10-17T23:00:03+00:00"},
    ]
    path = tmp_path / "conversation.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    atif = session_transcript_to_atif(
        read_transcript(path), extra_agent_fields={"version": "0.35.1"}
    )

    # Agent block: ATIF v1.7 requires both fields.
    assert atif["agent"]["name"] == "gptme"
    assert atif["agent"]["version"] == "0.35.1"

    # Every emitted metric key must be one ATIF's extra="forbid" model accepts.
    atif_metric_keys = {
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "cost_usd",
        "prompt_token_ids",
        "completion_token_ids",
        "logprobs",
        "extra",
    }
    metrics = [s["metrics"] for s in atif["steps"] if s.get("metrics")]
    assert metrics, "usage in the source records must survive the conversion"
    for block in metrics:
        assert set(block) <= atif_metric_keys
    assert metrics[0]["prompt_tokens"] == 1499 + 4224
    assert metrics[0]["cached_tokens"] == 4224

    # Step ids stay sequential from 1 (ATIF validator requirement).
    assert [s["step_id"] for s in atif["steps"]] == list(range(1, len(atif["steps"]) + 1))


def test_gptme_prose_preserved_alongside_markdown_tool_call(tmp_path):
    """Prose around a markdown-fence tool call survives into the ATIF step.

    gptme uses two formats for tool calls in conversation.jsonl content strings:
    - AT-format: ``@tool(id): {json}``
    - Markdown codeblock: ````lang\\ncontent\\n````

    A parser that handles one format must not silently erase the other, and
    neither may drop the surrounding assistant prose. This test pins the
    markdown path end-to-end: the prose stays in ``message`` and the fenced
    block becomes a ``tool_calls`` entry rather than raw text.
    """
    import json

    from gptme_sessions.transcript import read_transcript

    records = [
        {"role": "system", "content": "You are gptme.", "timestamp": "2026-10-18T00:00:00+00:00"},
        {"role": "user", "content": "list files", "timestamp": "2026-10-18T00:00:01+00:00"},
        {
            "role": "assistant",
            "content": "Listing files.\n\n```bash\nls\n```",
            "timestamp": "2026-10-18T00:00:02+00:00",
            "metadata": {"usage": {"input_tokens": 10, "output_tokens": 6}},
        },
        {
            "role": "system",
            "content": "file1.py\nfile2.py",
            "timestamp": "2026-10-18T00:00:03+00:00",
        },
    ]
    path = tmp_path / "conversation.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    atif = session_transcript_to_atif(
        read_transcript(path), extra_agent_fields={"version": "0.35.1"}
    )

    agent_steps = [s for s in atif["steps"] if s["source"] == "agent"]
    assert len(agent_steps) >= 1, "at least one agent step must be emitted"

    # Prose survives; the fence markers themselves are consumed by the parser.
    step_messages = [s.get("message", "") for s in agent_steps]
    assert (
        "Listing files." in step_messages
    ), f"assistant prose was dropped from the ATIF step message; got {step_messages!r}"

    # The markdown fence is a tool call, not raw text.
    tool_steps = [s for s in agent_steps if s.get("tool_calls")]
    assert tool_steps, "markdown fenced block must be parsed into a tool call"
    tc = tool_steps[0]["tool_calls"][0]
    assert tc["function_name"] == "shell"  # bash -> shell canonicalization
    assert "ls" in tc["arguments"].get("content", "")


# gptme end-to-end: _normalize_gptme -> session_transcript_to_atif
# ---------------------------------------------------------------------------


def _gptme_to_atif(records: list) -> dict:
    """Helper: normalize gptme records and convert to ATIF."""
    from gptme_sessions.transcript import _normalize_gptme, SessionTranscript

    msgs = _normalize_gptme(records)
    t = SessionTranscript(
        schema_version=TRANSCRIPT_SCHEMA_VERSION,
        session_id="gptme-e2e",
        harness="gptme",
        trajectory_path="/dev/null",
        model="gptme/0.35.1",
        messages=msgs,
    )
    return session_transcript_to_atif(t)


def test_gptme_injected_context_is_not_a_tool_result():
    """Harness context after a tool-calling turn must not become a result.

    gptme appends ``<system_info>`` / ``<system_warning>`` / ``<budget:…>``
    records to a step, and in the native tool loop it stamps a ``call_id`` onto
    every system record in that turn. Neither makes them tool output.
    """
    from gptme_sessions.transcript import _normalize_gptme

    msgs = _normalize_gptme(
        [
            {
                "role": "assistant",
                "content": "```shell\nls -la\n```",
                "timestamp": "2026-03-01T10:00:00+00:00",
            },
            {
                "role": "system",
                "content": "Ran allowlisted command: `ls -la`\n```stdout\ntotal 0\n```",
                "timestamp": "2026-03-01T10:00:01+00:00",
            },
            {
                "role": "system",
                "content": "<system_info>The time is now 10:00. Time elapsed: 1min</system_info>",
                "timestamp": "2026-03-01T10:00:02+00:00",
            },
            {
                "role": "system",
                "content": "<system_warning>Token usage: 10/200</system_warning>",
                "call_id": "toolu_abc",
                "timestamp": "2026-03-01T10:00:03+00:00",
            },
            {
                "role": "system",
                "content": "<budget:token_budget>200000</budget:token_budget>",
                "call_id": "toolu_abc",
                "timestamp": "2026-03-01T10:00:04+00:00",
            },
        ]
    )

    assert msgs[1].role == "tool_result"  # the real output still links
    assert msgs[2].role == "system"
    assert msgs[3].role == "system"
    assert msgs[4].role == "system"
    # Injected context never claims a call ID, so it cannot be linked later.
    assert all(m.tool_call_id is None for m in msgs[2:])


def test_gptme_native_tool_call_in_atif():
    """@tool(id): {json} format produces tool_calls with linked observation."""
    atif = _gptme_to_atif(
        [
            {
                "role": "assistant",
                "content": '@shell(s1): {"command": "ls"}',
                "timestamp": "2026-03-01T10:00:00+00:00",
            },
            {
                "role": "system",
                "content": "file.py",
                "call_id": "s1",
                "timestamp": "2026-03-01T10:00:01+00:00",
            },
        ]
    )
    agent_steps = [s for s in atif["steps"] if s["source"] == "agent"]
    assert len(agent_steps) == 1
    step = agent_steps[0]
    assert len(step["tool_calls"]) == 1
    assert step["tool_calls"][0]["tool_call_id"] == "s1"
    assert step["tool_calls"][0]["function_name"] == "shell"
    assert step["tool_calls"][0]["arguments"] == {"command": "ls"}
    assert len(step["observation"]["results"]) == 1
    assert step["observation"]["results"][0]["source_call_id"] == "s1"
    assert step["observation"]["results"][0]["content"] == "file.py"


def test_gptme_markdown_tool_call_in_atif():
    """Markdown fenced code blocks produce tool_calls with implicit observation."""
    atif = _gptme_to_atif(
        [
            {
                "role": "assistant",
                "content": "```shell\necho hello\n```",
                "timestamp": "2026-03-01T10:00:00+00:00",
            },
            {
                "role": "system",
                "content": "hello",
                "timestamp": "2026-03-01T10:00:01+00:00",
            },
        ]
    )
    agent_steps = [s for s in atif["steps"] if s["source"] == "agent"]
    assert len(agent_steps) == 1
    step = agent_steps[0]
    assert len(step["tool_calls"]) == 1
    tc = step["tool_calls"][0]
    assert tc["function_name"] == "shell"
    assert "echo hello" in tc["arguments"].get("content", "")
    assert "observation" in step


def test_gptme_xml_tool_call_in_atif():
    """<tool-use> XML format produces tool_calls."""
    atif = _gptme_to_atif(
        [
            {
                "role": "assistant",
                "content": "<tool-use><shell>printf hi</shell></tool-use>",
                "timestamp": "2026-03-01T10:00:00+00:00",
            },
            {
                "role": "system",
                "content": "hi",
                "timestamp": "2026-03-01T10:00:01+00:00",
            },
        ]
    )
    agent_steps = [s for s in atif["steps"] if s["source"] == "agent"]
    assert len(agent_steps) == 1
    step = agent_steps[0]
    assert len(step["tool_calls"]) == 1
    assert step["tool_calls"][0]["function_name"] == "shell"
    assert "observation" in step


def test_gptme_no_tool_calls_in_plain_conversation():
    """A conversation without tool calls produces zero tool_calls in ATIF."""
    atif = _gptme_to_atif(
        [
            {"role": "user", "content": "Hi", "timestamp": "2026-03-01T10:00:00+00:00"},
            {"role": "assistant", "content": "Hello!", "timestamp": "2026-03-01T10:01:00+00:00"},
        ]
    )
    agent_steps = [s for s in atif["steps"] if s["source"] == "agent"]
    assert len(agent_steps) == 1
    assert "tool_calls" not in agent_steps[0]
