"""Regression coverage for current Codex and Grok wire formats."""

import json

from gptme_sessions.transcript import _normalize_codex, _normalize_grok


def test_codex_custom_calls_and_developer_role():
    records = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "Run tests before shipping."}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": "*** Begin Patch\n*** Add File: test.py\n+pass\n*** End Patch",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "output": "Success. Updated test.py",
            },
        },
    ]
    messages = _normalize_codex(records)
    assert [m.role for m in messages] == ["system", "assistant", "tool_result"]
    assert messages[1].tool_name == "apply_patch"
    assert messages[1].tool_input == {"raw": records[1]["payload"]["input"]}
    assert messages[2].tool_result == "Success. Updated test.py"


def test_codex_exec_output_unwraps_json_chunks():
    output = "Script completed\nOutput:\n" + json.dumps(
        {
            "value": {"exit_code": 1, "output": "FAILED test_example\n1 failed\n"},
        }
    )
    messages = _normalize_codex(
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "output": output,
                },
            }
        ]
    )
    assert len(messages) == 1
    assert "FAILED test_example\n1 failed" in messages[0].tool_result
    assert messages[0].is_error is True


def _codex_block_output(*texts: str) -> dict:
    # Live shape (codex-cli, 2026-10): output is a list of input_text blocks.
    return {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call_output",
            "call_id": "call_blk",
            "output": [{"type": "input_text", "text": t} for t in texts],
        },
    }


def test_codex_block_output_unwraps_exec_chunks():
    chunk = json.dumps({"chunk_id": "52ac55", "exit_code": 0, "output": "/home/bob/bob\n"})
    messages = _normalize_codex(
        [_codex_block_output("Script completed\nWall time 0.1 seconds\nOutput:\n", chunk)]
    )
    assert len(messages) == 1
    assert "/home/bob/bob" in messages[0].tool_result
    assert "input_text" not in messages[0].tool_result
    assert messages[0].is_error is False


def test_codex_block_output_script_failed_is_error():
    messages = _normalize_codex(
        [
            _codex_block_output(
                "Script failed\nWall time 0.0 seconds\nOutput:\n",
                "Script error:\napply_patch verification failed: Failed to find expected lines",
            )
        ]
    )
    assert len(messages) == 1
    assert "apply_patch verification failed" in messages[0].tool_result
    assert messages[0].is_error is True


def test_codex_call_id_links_request_and_result():
    messages = _normalize_codex(
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "call_blk",
                    "input": "pwd",
                },
            },
            _codex_block_output("Script completed\nOutput:\n", "/home/bob/bob"),
        ]
    )
    assert [m.tool_call_id for m in messages] == ["call_blk", "call_blk"]


def test_grok_data_deltas_and_content_result():
    messages = _normalize_grok(
        [
            {"type": "thought", "data": "Inspect the test."},
            {"type": "text", "data": "Running tests."},
            {
                "type": "tool_call",
                "toolName": "run_terminal_command",
                "toolCallId": "tc1",
                "rawInput": {"command": "pytest"},
            },
            {
                "type": "tool_call_update",
                "toolCallId": "tc1",
                "status": "in_progress",
                "content": [{"content": {"text": "collecting"}}],
            },
            {
                "type": "tool_call_update",
                "toolCallId": "tc1",
                "status": "completed",
                "content": [{"content": {"text": "1 passed"}}, {"content": {"text": "Done."}}],
            },
        ]
    )
    assert [m.content for m in messages[:2]] == ["Inspect the test.", "Running tests."]
    assert messages[2].tool_input == {"command": "pytest"}
    assert len(messages) == 4
    assert messages[3].tool_result == "1 passed\nDone."


def test_grok_content_preferred_over_raw_output_and_error_retained():
    messages = _normalize_grok(
        [
            {
                "type": "tool_call_update",
                "status": "completed",
                "content": [{"content": {"text": "1 failed"}}],
                "rawOutput": {"exit_code": 1, "output_for_prompt": "fallback"},
            }
        ]
    )
    assert messages[0].tool_result == "1 failed"
    assert messages[0].is_error is True


def test_grok_top_level_error_is_preserved():
    messages = _normalize_grok(
        [
            {
                "type": "error",
                "message": "API error (status 402): usage balance exhausted",
            }
        ]
    )

    assert len(messages) == 1
    assert messages[0].role == "system"
    assert messages[0].content == "API error (status 402): usage balance exhausted"
    assert messages[0].is_error is True
