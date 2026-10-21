"""Copilot commit detection must scan the full bash result when the command is a commit command.

Regression for copilot sessions that chain work into one bash call, e.g.:
  git diff --stat && git-safe-commit --scope-only ... -m '...' 2>&1 | tail -3

The ``[branch hash] msg`` line is pushed past the 500-byte window by the prefix
output. The fix gates the full scan on the command being a commit command;
non-commit commands keep the 500-byte window to avoid false positives from
``git log --stat`` or file-read output.
"""

import json


from gptme_sessions.signals import extract_signals_copilot, is_productive


def _copilot_bash(
    call_id: str,
    command: str,
    result_content: str,
    *,
    success: bool = True,
) -> list[dict]:
    """Build a minimal copilot trajectory with one bash tool call."""
    return [
        {
            "type": "session.start",
            "data": {"sessionId": "test", "producer": "copilot-agent"},
            "timestamp": "2026-10-21T10:00:00Z",
        },
        {
            "type": "assistant.message",
            "data": {
                "toolRequests": [
                    {
                        "toolCallId": call_id,
                        "name": "bash",
                        "arguments": {"command": command},
                        "type": "function",
                    }
                ]
            },
            "timestamp": "2026-10-21T10:00:01Z",
        },
        {
            "type": "tool.execution_complete",
            "data": {
                "toolCallId": call_id,
                "success": success,
                "result": {"content": result_content},
            },
            "timestamp": "2026-10-21T10:00:05Z",
        },
    ]


# ---- helpers ----------------------------------------------------------------


def _commit_line_at_offset(offset: int) -> str:
    """Build a result string whose commit line starts at ``offset``."""
    padding = "x" * offset
    return padding + "[master abc1234] feat: add thing\n 1 file changed, 5 insertions(+)\n"


# ---- tests ------------------------------------------------------------------


def test_commit_command_scans_full_content():
    """A commit line past offset 500 is detected when the command is a commit command."""
    commit_cmd = (
        "git diff --stat -- some/file.py && "
        "git-safe-commit --scope-only some/file.py -m 'feat: add thing' 2>&1 | tail -3"
    )
    result = _commit_line_at_offset(600)
    msgs = _copilot_bash("tc_1", commit_cmd, result)
    signals = extract_signals_copilot(msgs)
    assert len(signals["git_commits"]) == 1
    assert "feat: add thing (abc1234)" in signals["git_commits"]
    assert is_productive(signals)


def test_commit_command_git_commit_scans_full_content():
    """Plain `git commit` (not just git-safe-commit) also triggers full scan."""
    commit_cmd = 'git add . && git commit -m "fix: typo" 2>&1 | tail -2'
    result = _commit_line_at_offset(700)
    msgs = _copilot_bash("tc_1", commit_cmd, result)
    signals = extract_signals_copilot(msgs)
    assert len(signals["git_commits"]) == 1
    assert "feat: add thing (abc1234)" in signals["git_commits"]


def test_non_commit_command_keeps_500_byte_window():
    """A commit-shaped line past offset 500 is NOT recorded for a non-commit command."""
    non_commit_cmd = "git log --stat -5"
    result = _commit_line_at_offset(600)
    msgs = _copilot_bash("tc_1", non_commit_cmd, result)
    signals = extract_signals_copilot(msgs)
    assert signals["git_commits"] == []
    assert not is_productive(signals)


def test_non_commit_command_inside_window_still_recorded():
    """A commit-shaped line inside the 500-byte window is still recorded for non-commit commands."""
    non_commit_cmd = "git status && git log -1"
    result = "[master def5678] docs: update README\n 1 file changed\n"
    msgs = _copilot_bash("tc_1", non_commit_cmd, result)
    signals = extract_signals_copilot(msgs)
    assert len(signals["git_commits"]) == 1
    assert "docs: update README (def5678)" in signals["git_commits"]


def test_commit_line_at_exactly_500_bytes_is_detected_by_commit_command():
    """Commit line starting exactly at offset 500 is found for commit commands."""
    commit_cmd = "git commit -m 'chore: bump' 2>&1 | tail -2"
    result = _commit_line_at_offset(500)
    msgs = _copilot_bash("tc_1", commit_cmd, result)
    signals = extract_signals_copilot(msgs)
    assert len(signals["git_commits"]) == 1


def test_commit_line_at_exactly_500_bytes_not_found_for_non_commit():
    """Commit line starting at offset 500 is NOT found for a non-commit command."""
    non_commit_cmd = "cat some_file.py"
    result = _commit_line_at_offset(500)
    msgs = _copilot_bash("tc_1", non_commit_cmd, result)
    signals = extract_signals_copilot(msgs)
    assert signals["git_commits"] == []


def test_arguments_as_json_string_is_parsed():
    """When copilot serializes arguments as a JSON string, commit detection still works."""
    commit_cmd = "git commit -m 'fix: args as string'"
    result = _commit_line_at_offset(600)
    # Copilot sometimes emits arguments as a JSON-encoded string
    msgs = [
        {
            "type": "session.start",
            "data": {"sessionId": "test", "producer": "copilot-agent"},
            "timestamp": "2026-10-21T10:00:00Z",
        },
        {
            "type": "assistant.message",
            "data": {
                "toolRequests": [
                    {
                        "toolCallId": "tc_2",
                        "name": "bash",
                        "arguments": json.dumps({"command": commit_cmd}),
                        "type": "function",
                    }
                ]
            },
            "timestamp": "2026-10-21T10:00:01Z",
        },
        {
            "type": "tool.execution_complete",
            "data": {
                "toolCallId": "tc_2",
                "success": True,
                "result": {"content": result},
            },
            "timestamp": "2026-10-21T10:00:05Z",
        },
    ]
    signals = extract_signals_copilot(msgs)
    assert len(signals["git_commits"]) == 1
    assert "feat: add thing (abc1234)" in signals["git_commits"]
