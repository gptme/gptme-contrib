"""Grok variant payloads must retain unambiguous tool result text."""

from pathlib import Path

import pytest

from gptme_sessions.transcript import _grok_output, _normalize_grok, read_transcript


def test_todo_write_fixture_preserves_summary_and_call_pairing():
    # Redacted and shortened from a real Grok Build todo_write call/update pair.
    path = Path(__file__).parent / "fixtures" / "grok" / "todo-write.jsonl"
    transcript = read_transcript(path)
    assert transcript.harness == "grok"
    call, result = transcript.messages
    assert call.tool_name == "todo_write"
    assert call.tool_call_id == result.tool_call_id == "todo-1"
    assert result.role == "tool_result"
    assert (
        result.content
        == result.tool_result
        == ("- [in_progress] 1: Run tests\n- [pending] 2: Commit changes\n")
    )
    assert not result.is_error


@pytest.mark.parametrize(
    "raw, expected",
    [
        ({"OtherVariant": {"message": "saved", "count": 1}}, "saved"),
        ({"OtherVariant": {"message": "saved", "detail": "more"}}, ""),
        ({"First": {"message": "one"}, "Second": {"message": "two"}}, ""),
        ({"OtherVariant": {"message": "", "count": 1}}, ""),
        ({"OtherVariant": {"state": {"id": "metadata"}}}, ""),
        ({"type": "Monitor", "task_id": "background-id"}, ""),
        ({"type": "TaskOutput", "Result": {"task_id": "background-id"}}, ""),
        ({"output": "inline", "OtherVariant": {"message": "fallback"}}, "inline"),
        (
            {"Result": {"output": "polled"}, "OtherVariant": {"message": "fallback"}},
            "polled",
        ),
    ],
)
def test_variant_fallback_is_unambiguous_and_respects_existing_output(raw, expected):
    assert _grok_output(raw) == (expected, False)


def test_content_takes_precedence_over_variant_summary_and_preserves_error():
    messages = _normalize_grok(
        [
            {
                "type": "tool_call_update",
                "toolCallId": "todo-1",
                "status": "completed",
                "content": [{"content": {"text": "explicit content"}}],
                "rawOutput": {
                    "exit_code": 1,
                    "TodosUpdated": {"summary_for_prompt": "fallback"},
                },
            }
        ]
    )
    assert messages[0].tool_result == "explicit content"
    assert messages[0].is_error
