"""Grok evidence must survive parallel calls and background command updates."""

from gptme_sessions.transcript import _normalize_grok


def call(ident, command):
    return {
        "type": "tool_call",
        "toolCallId": ident,
        "toolName": "run_terminal_command",
        "rawInput": {"command": command},
    }


def update(ident, text, status="completed", **raw):
    return {
        "type": "tool_call_update",
        "toolCallId": ident,
        "status": status,
        "content": [{"content": {"text": text}}],
        "rawOutput": raw,
    }


def test_out_of_order_results_carry_call_ids():
    messages = _normalize_grok(
        [
            call("commit", "git commit"),
            call("test", "pytest"),
            update("test", "1 passed"),
            update("commit", "[master abc1234] fix: bug"),
        ]
    )
    assert [m.tool_call_id for m in messages] == ["commit", "test", "test", "commit"]
    assert messages[2].to_dict()["tool_call_id"] == "test"


def test_late_progress_preserves_background_commit_output_once():
    output = "[master abc1234] fix: bug\n1 file changed"
    messages = _normalize_grok(
        [
            call("commit", "git-safe-commit"),
            update("commit", "Moved to background", type="BackgroundTaskStarted", task_id="bg"),
            update("commit", output, "in_progress"),
            update("commit", "short status", "in_progress"),
        ]
    )
    results = [m for m in messages if m.role == "tool_result"]
    assert len(results) == 1
    assert results[0].content == output


def test_completed_output_replaces_longer_progress_snapshot():
    messages = _normalize_grok(
        [
            call("test", "pytest"),
            update("test", "collecting many tests...", "in_progress"),
            update("test", "1 failed", exit_code=1),
        ]
    )
    assert len(messages) == 2
    assert messages[-1].content == "1 failed"
    assert messages[-1].is_error


def test_final_completion_clears_earlier_error_flag():
    messages = _normalize_grok(
        [
            call("test", "pytest"),
            update("test", "1 failed", "in_progress", exit_code=1),
            update("test", "0 failed", exit_code=0),
        ]
    )
    results = [m for m in messages if m.role == "tool_result"]
    assert len(results) == 1
    assert results[-1].content == "0 failed"
    assert not results[-1].is_error


def test_empty_completed_envelope_still_accepts_later_output():
    # Grok emits an empty ``completed`` envelope (e.g. Monitor calls) whose real
    # output arrives afterwards as ``in_progress``; that must not be finalized.
    messages = _normalize_grok(
        [
            call("monitor", "watch.sh"),
            update("monitor", "", type="Monitor"),
            update("monitor", "DONE: artifact written", "in_progress"),
        ]
    )
    results = [m for m in messages if m.role == "tool_result"]
    assert len(results) == 1
    assert results[-1].content == "DONE: artifact written"


def test_task_poll_restore_is_not_finalized_by_later_growth():
    messages = _normalize_grok(
        [
            call("build", "cargo build"),
            update(
                "build",
                "Command moved to background",
                type="BackgroundTaskStarted",
                task_id="bg",
            ),
            update("build", "first snapshot", "in_progress"),
            {
                "type": "tool_call",
                "toolCallId": "poll",
                "toolName": "get_command_or_subagent_output",
                "rawInput": {"task_id": "bg"},
            },
            {
                "type": "tool_call_update",
                "toolCallId": "poll",
                "status": "completed",
                "rawOutput": {
                    "type": "TaskOutput",
                    "Result": {"task_id": "bg", "output": "short poll snapshot"},
                },
            },
            update("build", "first snapshot plus more output", "in_progress"),
        ]
    )
    by_id = {m.tool_call_id: m for m in messages if m.role == "tool_result"}
    assert by_id["build"].content == "first snapshot plus more output"


def test_growing_background_output_keeps_latest_largest_snapshot():
    # Real Grok background output streams as growing, cumulative in_progress
    # snapshots; the first is partial, so later larger snapshots must enrich it.
    messages = _normalize_grok(
        [
            call("build", "cargo build"),
            update(
                "build",
                "Command moved to background",
                type="BackgroundTaskStarted",
                task_id="bg",
            ),
            update("build", "Compiling...", "in_progress"),
            update("build", "Compiling...DONE", "in_progress"),
        ]
    )
    results = [m for m in messages if m.role == "tool_result"]
    assert len(results) == 1
    assert results[-1].content == "Compiling...DONE"


def test_late_progress_after_completion_does_not_clobber_final_result():
    messages = _normalize_grok(
        [
            call("test", "pytest"),
            update("test", "collecting many tests...", "in_progress"),
            update("test", "1 failed", exit_code=1),
            update("test", "still running... very long stale progress", "in_progress"),
        ]
    )
    results = [m for m in messages if m.role == "tool_result"]
    assert len(results) == 1
    assert results[-1].content == "1 failed"
    assert results[-1].is_error


def test_task_poll_restores_original_command_result_even_when_shorter():
    messages = _normalize_grok(
        [
            call("commit", "git-safe-commit"),
            update(
                "commit",
                "Command moved to background with a very long status message",
                type="BackgroundTaskStarted",
                task_id="bg",
            ),
            {
                "type": "tool_call",
                "toolCallId": "poll",
                "toolName": "get_command_or_subagent_output",
                "rawInput": {"task_id": "bg"},
            },
            {
                "type": "tool_call_update",
                "toolCallId": "poll",
                "status": "completed",
                "rawOutput": {
                    "type": "TaskOutput",
                    "Result": {
                        "task_id": "bg",
                        "output": "[master abc1234] fix: bug",
                        "exit_code": 1,
                    },
                },
            },
        ]
    )
    by_id = {m.tool_call_id: m for m in messages if m.role == "tool_result"}
    assert by_id["commit"].content == "[master abc1234] fix: bug"
    assert by_id["commit"].is_error
    assert by_id["poll"].is_error


def test_short_late_progress_replaces_background_placeholder():
    messages = _normalize_grok(
        [
            call("test", "pytest"),
            update(
                "test",
                "Command moved to background with a very long status message",
                type="BackgroundTaskStarted",
                task_id="bg",
            ),
            update("test", "1 passed", "in_progress"),
        ]
    )
    assert messages[-1].content == "1 passed"


def test_task_output_dict_and_list_preserve_output_and_errors():
    for result in (
        {"output": "FAILED test_bug", "exit_code": 1},
        [{"output": "FAILED test_bug", "exit_code": 1}],
    ):
        messages = _normalize_grok(
            [
                {
                    "type": "tool_call_update",
                    "toolCallId": "poll",
                    "status": "completed",
                    "rawOutput": {"type": "TaskOutput", "Result": result},
                }
            ]
        )
        assert messages[0].content == "FAILED test_bug"
        assert messages[0].is_error


def test_deltas_join_without_losing_spaces_and_thoughts_are_tagged():
    messages = _normalize_grok(
        [
            {"type": "thought", "data": "Private "},
            {"type": "thought", "data": "reasoning."},
            {"type": "text", "data": "Fast-forwarded "},
            {"type": "text", "data": "two commits."},
            call("test", "pytest"),
            {"type": "text", "data": "Done."},
        ]
    )
    assert [m.content for m in messages] == [
        "Private reasoning.",
        "Fast-forwarded two commits.",
        "",
        "Done.",
    ]
    assert messages[0].is_reasoning
    assert not messages[1].is_reasoning
