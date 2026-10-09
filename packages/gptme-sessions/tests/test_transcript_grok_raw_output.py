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
        ({"OtherVariant": {"message": "saved", "detail": ""}}, ""),
        (
            {"FileContent": {"content": "", "content_concise": "", "absolute_path": "/file"}},
            "",
        ),
        (
            {
                "type": "ReadFile",
                "FileContent": {
                    "content": "1→hello\n",
                    "content_concise": "hello\n",
                    "absolute_path": "/file",
                    "raw_output": "hello\n",
                    "total_lines": 1,
                },
            },
            "1→hello\n",
        ),
        (
            {
                "type": "GrepSearch",
                "exit_code": 0,
                "file_matches": [],
                "match_count": 1,
                "stderr": [],
                "stdout": list(b"Found 1 matching line\n/file.py\n"),
            },
            "Found 1 matching line\n/file.py\n",
        ),
        (
            {"output": list(b"inline"), "OtherVariant": {"message": "fallback"}},
            "inline",
        ),
        (
            {
                "type": "SearchReplace",
                "EditsApplied": {
                    "old_string": "before",
                    "new_string": "after",
                    "tool_output_for_prompt": "The file /tmp/x.py has been updated successfully.",
                    "tool_output_for_prompt_concise": "The file /tmp/x.py has been updated successfully.",
                    "absolute_path": "/tmp/x.py",
                },
            },
            "The file /tmp/x.py has been updated successfully.",
        ),
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


def test_grep_search_stderr_and_nonzero_exit():
    raw = {
        "type": "GrepSearch",
        "exit_code": 2,
        "file_matches": [],
        "match_count": 0,
        "stderr": list(b"rg: regex parse error"),
        "stdout": [],
    }
    assert _grok_output(raw) == ("rg: regex parse error", True)


def test_read_file_variant_pairs_through_normalize():
    messages = _normalize_grok(
        [
            {
                "type": "tool_call",
                "toolCallId": "read-1",
                "toolName": "read_file",
                "rawInput": {"target_file": "/file"},
            },
            {
                "type": "tool_call_update",
                "toolCallId": "read-1",
                "status": "completed",
                "rawOutput": {
                    "type": "ReadFile",
                    "FileContent": {
                        "content": "1→body\n",
                        "content_concise": "body\n",
                        "absolute_path": "/file",
                        "raw_output": "body\n",
                        "total_lines": 1,
                    },
                },
            },
        ]
    )
    call, result = messages
    assert call.tool_name == "read_file"
    assert call.tool_call_id == result.tool_call_id == "read-1"
    assert result.tool_result == result.content == "1→body\n"
    assert not result.is_error


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
