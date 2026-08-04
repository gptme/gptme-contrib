"""Unit tests for Grok Build streaming-json signal extraction.

Grok Build records are NDJSON with typed fields:
  available_commands, thought, tool_call, tool_call_update, text, usage, end.

The extractor detects this format via the opening `available_commands` record
and dispatches to extract_signals_grok / extract_usage_grok.
"""

from __future__ import annotations

from gptme_sessions.signals import (
    _detect_format,
    extract_signals_grok,
    extract_usage_grok,
)

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _available_commands() -> dict:
    return {
        "type": "available_commands",
        "commands": ["run_terminal_command", "write", "search_replace", "read"],
    }


def _thought(text: str = "thinking...") -> dict:
    return {"type": "thought", "content": text}


def _tool_call(
    tool_name: str,
    raw_input: dict,
    call_id: str = "tc1",
) -> dict:
    return {
        "type": "tool_call",
        "toolName": tool_name,
        "toolCallId": call_id,
        "rawInput": raw_input,
    }


def _tool_call_update(
    call_id: str = "tc1",
    status: str = "completed",
    raw_output: dict | None = None,
    exit_code: int = 0,
) -> dict:
    output = (
        raw_output if raw_output is not None else {"exit_code": exit_code, "output_for_prompt": ""}
    )
    return {
        "type": "tool_call_update",
        "toolCallId": call_id,
        "status": status,
        "rawOutput": output,
    }


def _text(content: str = "Done.") -> dict:
    return {"type": "text", "content": content}


def _end_record(
    input_tokens: int = 100, output_tokens: int = 50, model: str = "grok-4.5-build"
) -> dict:
    return {
        "type": "end",
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": 0,
        },
        "modelUsage": {model: {"input_tokens": input_tokens, "output_tokens": output_tokens}},
    }


# ─── _detect_format ──────────────────────────────────────────────────────────


def test_detect_format_grok_on_available_commands_first_record():
    msgs = [_available_commands(), _thought()]
    assert _detect_format(msgs) == "grok"


def test_detect_format_grok_not_triggered_by_other_types():
    """Non-grok streams (gptme, CC) must not be mis-detected as grok."""
    cc_msgs = [
        {
            "type": "system",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "system", "content": "You are an AI."},
        },
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {"role": "assistant", "content": []},
        },
    ]
    fmt = _detect_format(cc_msgs)
    assert fmt != "grok"


def test_detect_format_empty_returns_gptme_default():
    # _detect_format falls back to 'gptme' when no records match any known format.
    assert _detect_format([]) == "gptme"


# ─── extract_signals_grok ────────────────────────────────────────────────────


def test_extract_signals_grok_empty_trajectory_returns_clean_structure():
    sigs = extract_signals_grok([])
    assert sigs["tool_calls"] == {}
    assert sigs["git_commits"] == []
    assert sigs["file_writes"] == []
    assert sigs["deliverables"] == []


def test_extract_signals_grok_run_terminal_command_counted():
    msgs = [
        _available_commands(),
        _tool_call("run_terminal_command", {"command": "ls -la"}, "tc1"),
        _tool_call_update("tc1", raw_output={"exit_code": 0, "output_for_prompt": "total 8\n"}),
    ]
    sigs = extract_signals_grok(msgs)
    assert sigs["tool_calls"]["run_terminal_command"] == 1
    assert sigs["git_commits"] == []
    assert sigs["file_writes"] == []


def test_extract_signals_grok_git_commit_detected_from_output():
    commit_output = "[master abc1234] feat: add grok extractor\n 3 files changed\n"
    msgs = [
        _available_commands(),
        _tool_call(
            "run_terminal_command",
            {"command": "git commit src/foo.py -m 'feat: add grok extractor'"},
            "tc1",
        ),
        _tool_call_update("tc1", raw_output={"exit_code": 0, "output_for_prompt": commit_output}),
        _text("Committed."),
    ]
    sigs = extract_signals_grok(msgs)
    assert len(sigs["git_commits"]) == 1
    assert "abc1234" in sigs["git_commits"][0]
    assert "abc1234" in sigs["deliverables"][0]


def test_extract_signals_grok_git_safe_commit_also_detected():
    """git-safe-commit output should be parsed the same as plain git commit."""
    commit_output = "[master f3ad1ab] fix: cleanup\n 1 file changed\n"
    msgs = [
        _available_commands(),
        _tool_call(
            "run_terminal_command",
            {"command": "git-safe-commit --scope-only file.py -m 'fix: cleanup'"},
            "tc1",
        ),
        _tool_call_update("tc1", raw_output={"exit_code": 0, "output_for_prompt": commit_output}),
    ]
    sigs = extract_signals_grok(msgs)
    assert any("f3ad1ab" in c for c in sigs["git_commits"])


def test_extract_signals_grok_no_commit_when_git_command_absent():
    """A run_terminal_command that does NOT call git commit must not produce a commit record."""
    commit_output = "[master abc9999] feat: thing"  # looks like commit but command differs
    msgs = [
        _available_commands(),
        _tool_call("run_terminal_command", {"command": "cat CHANGELOG.md"}, "tc1"),
        _tool_call_update("tc1", raw_output={"exit_code": 0, "output_for_prompt": commit_output}),
    ]
    sigs = extract_signals_grok(msgs)
    assert sigs["git_commits"] == []


def test_extract_signals_grok_write_tool_captured_as_file_write():
    msgs = [
        _available_commands(),
        _tool_call("write", {"path": "/home/bob/bob/scripts/foo.py", "content": "x=1"}, "tc1"),
        _tool_call_update("tc1", raw_output={"exit_code": 0, "output_for_prompt": "Written."}),
    ]
    sigs = extract_signals_grok(msgs)
    assert "/home/bob/bob/scripts/foo.py" in sigs["file_writes"]
    assert "/home/bob/bob/scripts/foo.py" in sigs["deliverables"]


def test_extract_signals_grok_search_replace_captured_as_file_write():
    msgs = [
        _available_commands(),
        _tool_call(
            "search_replace", {"path": "/home/bob/bob/AGENTS.md", "old": "foo", "new": "bar"}, "tc1"
        ),
        _tool_call_update("tc1", raw_output={"exit_code": 0, "output_for_prompt": "Replaced."}),
    ]
    sigs = extract_signals_grok(msgs)
    assert "/home/bob/bob/AGENTS.md" in sigs["file_writes"]


def test_extract_signals_grok_journal_path_goes_to_journal_paths_not_file_writes():
    msgs = [
        _available_commands(),
        _tool_call(
            "write",
            {
                "path": "/home/bob/bob/journal/2026-08-04/autonomous-session-f3ad.md",
                "content": "# Session",
            },
            "tc1",
        ),
        _tool_call_update("tc1", raw_output={"exit_code": 0, "output_for_prompt": "Written."}),
    ]
    sigs = extract_signals_grok(msgs)
    assert "/home/bob/bob/journal/2026-08-04/autonomous-session-f3ad.md" in sigs["journal_paths"]
    assert "/home/bob/bob/journal/2026-08-04/autonomous-session-f3ad.md" not in sigs["file_writes"]


def test_extract_signals_grok_non_completed_update_skipped():
    """tool_call_update with status != 'completed' must not be parsed for output."""
    msgs = [
        _available_commands(),
        _tool_call("run_terminal_command", {"command": "git commit file.py -m 'feat: x'"}, "tc1"),
        # status=running — should be ignored even if output looks like a commit
        {
            "type": "tool_call_update",
            "toolCallId": "tc1",
            "status": "running",
            "rawOutput": {"exit_code": 0, "output_for_prompt": "[master abc0000] feat: x"},
        },
    ]
    sigs = extract_signals_grok(msgs)
    assert sigs["git_commits"] == []


def test_extract_signals_grok_error_count_incremented_on_nonzero_exit():
    msgs = [
        _available_commands(),
        _tool_call("run_terminal_command", {"command": "make test"}, "tc1"),
        _tool_call_update("tc1", raw_output={"exit_code": 1, "output_for_prompt": "FAILED"}),
    ]
    sigs = extract_signals_grok(msgs)
    assert sigs["error_count"] >= 1


def test_extract_signals_grok_multiple_tool_calls_aggregated():
    msgs = [
        _available_commands(),
        _tool_call("run_terminal_command", {"command": "ls"}, "tc1"),
        _tool_call_update("tc1"),
        _tool_call("write", {"path": "/tmp/a.py", "content": ""}, "tc2"),
        _tool_call_update("tc2"),
        _tool_call("run_terminal_command", {"command": "cat /tmp/a.py"}, "tc3"),
        _tool_call_update("tc3"),
    ]
    sigs = extract_signals_grok(msgs)
    assert sigs["tool_calls"]["run_terminal_command"] == 2
    assert sigs["tool_calls"]["write"] == 1


def test_extract_signals_grok_duplicate_commits_deduplicated():
    """The same commit SHA appearing twice must yield only one deliverable."""
    commit_output = "[master aaabbb1] feat: thing"
    msgs = [
        _available_commands(),
        _tool_call("run_terminal_command", {"command": "git commit f.py -m 'feat: thing'"}, "tc1"),
        _tool_call_update("tc1", raw_output={"exit_code": 0, "output_for_prompt": commit_output}),
        _tool_call("run_terminal_command", {"command": "git commit -m 'feat: thing'"}, "tc2"),
        _tool_call_update("tc2", raw_output={"exit_code": 0, "output_for_prompt": commit_output}),
    ]
    sigs = extract_signals_grok(msgs)
    assert len(sigs["git_commits"]) == 1


# ─── extract_usage_grok ──────────────────────────────────────────────────────


def test_extract_usage_grok_reads_from_end_record():
    msgs = [
        _available_commands(),
        _text("Done."),
        _end_record(input_tokens=7170, output_tokens=114, model="grok-4.5-build"),
    ]
    usage = extract_usage_grok(msgs)
    assert usage["input_tokens"] == 7170
    assert usage["output_tokens"] == 114
    assert usage["model"] == "grok-4.5-build"


def test_extract_usage_grok_empty_trajectory():
    assert extract_usage_grok([]) == {}


def test_extract_usage_grok_no_end_record():
    msgs = [_available_commands(), _text("incomplete")]
    assert extract_usage_grok(msgs) == {}


def test_extract_usage_grok_cache_read_tokens_included():
    msgs = [
        _available_commands(),
        {
            "type": "end",
            "usage": {"input_tokens": 1000, "output_tokens": 50, "cache_read_input_tokens": 29184},
            "modelUsage": {"grok-4.5-build": {}},
        },
    ]
    usage = extract_usage_grok(msgs)
    assert usage["cache_read_input_tokens"] == 29184
