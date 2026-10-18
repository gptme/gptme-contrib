"""Tests for the normalized session transcript module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gptme_sessions.atif import session_transcript_to_atif
from gptme_sessions.transcript import (
    NormalizedMessage,
    TRANSCRIPT_SCHEMA_VERSION,
    _normalize_cc,
    _normalize_codex,
    _normalize_copilot,
    _normalize_gptme,
    _normalize_grok,
    read_transcript,
)


# ---------------------------------------------------------------------------
# Fixtures: minimal JSONL files per harness
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    """Write a list of dicts as JSONL to path."""
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


@pytest.fixture
def gptme_jsonl(tmp_path: Path) -> Path:
    records = [
        {
            "role": "user",
            "content": "Hello, please write a test",
            "timestamp": "2026-03-01T10:00:00+00:00",
        },
        {
            "role": "assistant",
            "content": '@save(c1): {"path": "test.py", "content": "# test"}',
            "timestamp": "2026-03-01T10:01:00+00:00",
        },
        {
            "role": "system",
            "content": "File saved to test.py",
            "timestamp": "2026-03-01T10:01:05+00:00",
        },
    ]
    return _write_jsonl(tmp_path / "conversation.jsonl", records)


@pytest.fixture
def cc_jsonl(tmp_path: Path) -> Path:
    records = [
        {
            "type": "assistant",
            "timestamp": "2026-03-01T10:00:00.000Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-6",
                "content": [
                    {"type": "text", "text": "I'll write a test file for you."},
                    {
                        "type": "tool_use",
                        "id": "tool_001",
                        "name": "Write",
                        "input": {"file_path": "test.py", "content": "# test"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-03-01T10:00:05.000Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool_001",
                        "content": "File written successfully",
                        "is_error": False,
                    }
                ],
            },
        },
    ]
    return _write_jsonl(tmp_path / "abc12345-0000-0000-0000-000000000001.jsonl", records)


@pytest.fixture
def codex_jsonl(tmp_path: Path) -> Path:
    records = [
        {
            "type": "session_meta",
            "timestamp": "2026-03-01T10:00:00.000Z",
            "payload": {"originator": "codex_exec", "model": "gpt-5.3-codex"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-03-01T10:00:01.000Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Writing test file..."}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-03-01T10:00:02.000Z",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call_001",
                "arguments": json.dumps({"cmd": "cat > test.py << 'EOF'\n# test\nEOF"}),
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-03-01T10:00:05.000Z",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_001",
                "output": "Process exited with code 0",
            },
        },
    ]
    return _write_jsonl(tmp_path / "rollout.jsonl", records)


@pytest.fixture
def copilot_jsonl(tmp_path: Path) -> Path:
    records = [
        {
            "type": "session.start",
            "timestamp": "2026-03-01T10:00:00.000Z",
            "data": {"producer": "copilot-agent", "selectedModel": "gpt-5.4"},
        },
        {
            "type": "assistant.message",
            "timestamp": "2026-03-01T10:00:01.000Z",
            "data": {
                "text": "Writing a test file.",
                "toolRequests": [
                    {
                        "name": "write",
                        "toolCallId": "tc_001",
                        "arguments": {"path": "test.py", "content": "# test"},
                    }
                ],
            },
        },
        {
            "type": "tool.execution_complete",
            "timestamp": "2026-03-01T10:00:03.000Z",
            "data": {
                "toolCallId": "tc_001",
                "success": True,
                "result": {"content": "File written"},
            },
        },
    ]
    return _write_jsonl(tmp_path / "events.jsonl", records)


@pytest.fixture
def grok_jsonl(tmp_path: Path) -> Path:
    records = [
        {"type": "available_commands", "commands": ["run_terminal_command", "write"]},
        {"type": "thought", "content": "I will write a test file."},
        {
            "type": "tool_call",
            "toolName": "write",
            "toolCallId": "tc1",
            "rawInput": {"path": "test.py", "content": "# test"},
        },
        {
            "type": "tool_call_update",
            "toolCallId": "tc1",
            "status": "completed",
            "rawOutput": {"exit_code": 0, "output_for_prompt": "File written."},
        },
        {"type": "text", "content": "Done."},
        {
            "type": "end",
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "modelUsage": {"grok-4.5-build": {"input_tokens": 100, "output_tokens": 50}},
        },
    ]
    return _write_jsonl(tmp_path / "session.jsonl", records)


# ---------------------------------------------------------------------------
# Unit tests for normalizers
# ---------------------------------------------------------------------------


class TestNormalizeGptme:
    def test_basic_roles(self, gptme_jsonl: Path):
        records = [json.loads(line) for line in gptme_jsonl.read_text().strip().splitlines()]
        norm = _normalize_gptme(records)
        # user + assistant tool call + tool_result (system → tool_result after a call)
        assert len(norm) == 3
        assert norm[0].role == "user"
        assert norm[1].role == "assistant"
        assert norm[2].role == "tool_result"

    def test_content_preserved(self, gptme_jsonl: Path):
        records = [json.loads(line) for line in gptme_jsonl.read_text().strip().splitlines()]
        norm = _normalize_gptme(records)
        assert "Hello" in norm[0].content
        # tool call message carries the structured input, not raw text
        assert norm[1].tool_input is not None
        assert "test.py" in norm[1].tool_input.get("path", "")

    def test_timestamp_preserved(self, gptme_jsonl: Path):
        records = [json.loads(line) for line in gptme_jsonl.read_text().strip().splitlines()]
        norm = _normalize_gptme(records)
        assert norm[0].timestamp is not None
        assert "2026-03-01" in norm[0].timestamp

    def test_at_format_tool_call_parsed(self, gptme_jsonl: Path):
        records = [json.loads(line) for line in gptme_jsonl.read_text().strip().splitlines()]
        norm = _normalize_gptme(records)
        tool_msgs = [m for m in norm if m.tool_name]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].tool_name == "save"
        assert tool_msgs[0].tool_call_id == "c1"
        assert tool_msgs[0].tool_input == {"path": "test.py", "content": "# test"}

    def test_at_format_system_becomes_tool_result(self, gptme_jsonl: Path):
        records = [json.loads(line) for line in gptme_jsonl.read_text().strip().splitlines()]
        norm = _normalize_gptme(records)
        results = [m for m in norm if m.role == "tool_result"]
        assert len(results) == 1
        assert results[0].tool_result == "File saved to test.py"
        assert results[0].tool_call_id == "c1"

    def test_at_format_call_result_linked_via_call_id(self, gptme_jsonl: Path):
        """tool_call_id on call and result must match for ATIF source_call_id linking."""
        records = [json.loads(line) for line in gptme_jsonl.read_text().strip().splitlines()]
        norm = _normalize_gptme(records)
        call = next(m for m in norm if m.tool_name)
        result = next(m for m in norm if m.role == "tool_result")
        assert call.tool_call_id == result.tool_call_id == "c1"

    def test_noise_system_stays_system(self):
        records = [
            {"role": "assistant", "content": '@shell(s1): {"command": "ls"}'},
            {"role": "system", "content": "<system_warning>Low context</system_warning>"},
            {"role": "system", "content": "Shell exit 0"},
        ]
        norm = _normalize_gptme(records)
        sys_msgs = [m for m in norm if m.role == "system"]
        result_msgs = [m for m in norm if m.role == "tool_result"]
        assert len(sys_msgs) == 1
        assert "<system_warning>" in sys_msgs[0].content
        # The non-noise system message after the noise one is the result
        assert len(result_msgs) == 1
        assert result_msgs[0].content == "Shell exit 0"

    def test_pinned_system_stays_system(self):
        records = [
            {"role": "system", "content": "You are a helpful assistant.", "pinned": True},
            {"role": "assistant", "content": '@shell(s2): {"command": "pwd"}'},
            {"role": "system", "content": "/home/user"},
        ]
        norm = _normalize_gptme(records)
        sys_msgs = [m for m in norm if m.role == "system"]
        result_msgs = [m for m in norm if m.role == "tool_result"]
        assert len(sys_msgs) == 1
        assert "helpful assistant" in sys_msgs[0].content
        assert len(result_msgs) == 1
        assert result_msgs[0].content == "/home/user"

    def test_unlinked_system_stays_system(self):
        """A system message with no preceding tool call stays as system."""
        records = [
            {"role": "system", "content": "Workspace initialized."},
        ]
        norm = _normalize_gptme(records)
        assert norm[0].role == "system"

    def test_markdown_codeblock_tool_call(self):
        records = [
            {"role": "assistant", "content": "Running a command.\n\n```bash\nls -la\n```"},
            {"role": "system", "content": "total 8"},
        ]
        norm = _normalize_gptme(records)
        tool_msgs = [m for m in norm if m.tool_name]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].tool_name == "bash"
        assert tool_msgs[0].tool_input == {"content": "ls -la"}
        assert tool_msgs[0].tool_call_id is None
        result_msgs = [m for m in norm if m.role == "tool_result"]
        assert len(result_msgs) == 1
        assert result_msgs[0].content == "total 8"

    def test_xml_tool_call(self):
        records = [
            {
                "role": "assistant",
                "content": "I'll run this:\n<tool-use>\n<bash>echo hello</bash>\n</tool-use>",
            },
            {"role": "system", "content": "hello"},
        ]
        norm = _normalize_gptme(records)
        tool_msgs = [m for m in norm if m.tool_name]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].tool_name == "bash"
        assert tool_msgs[0].tool_input == {"content": "echo hello"}
        result_msgs = [m for m in norm if m.role == "tool_result"]
        assert len(result_msgs) == 1

    def test_text_before_at_call_preserved(self):
        records = [
            {
                "role": "assistant",
                "content": 'Writing the file now.\n\n@save(x1): {"path": "a.py", "content": "x"}',
            },
            {"role": "system", "content": "Saved."},
        ]
        norm = _normalize_gptme(records)
        text_msgs = [m for m in norm if m.role == "assistant" and not m.tool_name and m.content]
        assert len(text_msgs) == 1
        assert "Writing" in text_msgs[0].content
        tool_msgs = [m for m in norm if m.tool_name]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].tool_call_id == "x1"

    def test_usage_on_assistant_turn(self):
        records = [
            {
                "role": "assistant",
                "content": '@shell(u1): {"command": "ls"}',
                "metadata": {"usage": {"input_tokens": 10, "output_tokens": 5}},
            }
        ]
        norm = _normalize_gptme(records)
        tool_msgs = [m for m in norm if m.tool_name]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].usage == {"input_tokens": 10, "output_tokens": 5}


class TestNormalizeCc:
    def test_tool_use_emitted(self, cc_jsonl: Path):
        records = [json.loads(line) for line in cc_jsonl.read_text().strip().splitlines()]
        norm = _normalize_cc(records)
        tool_msgs = [m for m in norm if m.tool_name]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].tool_name == "Write"
        assert tool_msgs[0].role == "assistant"

    def test_tool_input_preserved(self, cc_jsonl: Path):
        records = [json.loads(line) for line in cc_jsonl.read_text().strip().splitlines()]
        norm = _normalize_cc(records)
        write_msg = next(m for m in norm if m.tool_name == "Write")
        assert write_msg.tool_input == {"file_path": "test.py", "content": "# test"}

    def test_tool_result_emitted(self, cc_jsonl: Path):
        records = [json.loads(line) for line in cc_jsonl.read_text().strip().splitlines()]
        norm = _normalize_cc(records)
        results = [m for m in norm if m.role == "tool_result"]
        assert len(results) == 1
        assert "written" in results[0].content.lower()
        assert results[0].is_error is False

    def test_text_turn_emitted(self, cc_jsonl: Path):
        records = [json.loads(line) for line in cc_jsonl.read_text().strip().splitlines()]
        norm = _normalize_cc(records)
        text_msgs = [m for m in norm if m.role == "assistant" and not m.tool_name and m.content]
        assert len(text_msgs) == 1
        assert "write a test" in text_msgs[0].content.lower()

    def test_text_turn_precedes_tool_calls(self, cc_jsonl: Path):
        records = [json.loads(line) for line in cc_jsonl.read_text().strip().splitlines()]
        norm = _normalize_cc(records)
        assert norm[0].role == "assistant"
        assert norm[0].tool_name is None
        assert "write a test" in norm[0].content.lower()
        assert norm[1].tool_name == "Write"

    def test_error_tool_result(self, tmp_path: Path):
        records = [
            {
                "type": "user",
                "timestamp": "2026-03-01T10:00:05.000Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_err",
                            "content": "Permission denied",
                            "is_error": True,
                        }
                    ],
                },
            }
        ]
        norm = _normalize_cc(records)
        assert len(norm) == 1
        assert norm[0].is_error is True


class TestNormalizeCodex:
    def test_message_turn(self, codex_jsonl: Path):
        records = [json.loads(line) for line in codex_jsonl.read_text().strip().splitlines()]
        norm = _normalize_codex(records)
        text_msgs = [m for m in norm if m.role == "assistant" and not m.tool_name and m.content]
        assert any("Writing" in m.content for m in text_msgs)

    def test_function_call(self, codex_jsonl: Path):
        records = [json.loads(line) for line in codex_jsonl.read_text().strip().splitlines()]
        norm = _normalize_codex(records)
        tool_msgs = [m for m in norm if m.tool_name]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].tool_name == "exec_command"

    def test_function_call_output(self, codex_jsonl: Path):
        records = [json.loads(line) for line in codex_jsonl.read_text().strip().splitlines()]
        norm = _normalize_codex(records)
        results = [m for m in norm if m.role == "tool_result"]
        assert len(results) == 1
        assert "exited" in results[0].content.lower()


class TestNormalizeGrok:
    def test_thought_emitted(self, grok_jsonl: Path):
        records = [json.loads(line) for line in grok_jsonl.read_text().strip().splitlines()]
        norm = _normalize_grok(records)
        text_msgs = [m for m in norm if m.role == "assistant" and not m.tool_name and m.content]
        assert any("write a test" in m.content.lower() for m in text_msgs)

    def test_tool_call_emitted(self, grok_jsonl: Path):
        records = [json.loads(line) for line in grok_jsonl.read_text().strip().splitlines()]
        norm = _normalize_grok(records)
        tool_msgs = [m for m in norm if m.tool_name]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].tool_name == "write"
        assert tool_msgs[0].tool_input == {"path": "test.py", "content": "# test"}

    def test_tool_result_emitted(self, grok_jsonl: Path):
        records = [json.loads(line) for line in grok_jsonl.read_text().strip().splitlines()]
        norm = _normalize_grok(records)
        results = [m for m in norm if m.role == "tool_result"]
        assert len(results) == 1
        assert "written" in results[0].content.lower()
        assert results[0].is_error is False

    def test_skips_available_commands_and_end(self, grok_jsonl: Path):
        records = [json.loads(line) for line in grok_jsonl.read_text().strip().splitlines()]
        norm = _normalize_grok(records)
        # available_commands and end records must not produce messages
        assert all(m.role in ("assistant", "tool_result") for m in norm)

    def test_error_tool_result(self):
        records = [
            {"type": "available_commands", "commands": ["run_terminal_command"]},
            {
                "type": "tool_call",
                "toolName": "run_terminal_command",
                "toolCallId": "tc_err",
                "rawInput": {"command": "ls /nonexistent"},
            },
            {
                "type": "tool_call_update",
                "toolCallId": "tc_err",
                "status": "completed",
                "rawOutput": {"exit_code": 1, "output_for_prompt": "ls: cannot access"},
            },
        ]
        norm = _normalize_grok(records)
        results = [m for m in norm if m.role == "tool_result"]
        assert len(results) == 1
        assert results[0].is_error is True


class TestNormalizeCopilot:
    def test_assistant_text(self, copilot_jsonl: Path):
        records = [json.loads(line) for line in copilot_jsonl.read_text().strip().splitlines()]
        norm = _normalize_copilot(records)
        text_msgs = [m for m in norm if m.role == "assistant" and not m.tool_name and m.content]
        assert len(text_msgs) == 1
        assert "Writing" in text_msgs[0].content

    def test_tool_request(self, copilot_jsonl: Path):
        records = [json.loads(line) for line in copilot_jsonl.read_text().strip().splitlines()]
        norm = _normalize_copilot(records)
        tool_msgs = [m for m in norm if m.tool_name]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].tool_name == "write"
        assert tool_msgs[0].tool_input == {"path": "test.py", "content": "# test"}

    def test_tool_result(self, copilot_jsonl: Path):
        records = [json.loads(line) for line in copilot_jsonl.read_text().strip().splitlines()]
        norm = _normalize_copilot(records)
        results = [m for m in norm if m.role == "tool_result"]
        assert len(results) == 1
        assert results[0].is_error is False

    def test_tool_call_id_links_request_and_result(self, copilot_jsonl: Path):
        records = [json.loads(line) for line in copilot_jsonl.read_text().strip().splitlines()]
        norm = _normalize_copilot(records)
        request = next(m for m in norm if m.tool_name)
        result = next(m for m in norm if m.role == "tool_result")
        assert request.tool_call_id == "tc_001"
        assert result.tool_call_id == "tc_001"

    def test_failed_result_keeps_error_message(self):
        # Live shape (copilot-cli, 2026-10): failures carry no `result`, only `error`.
        records = [
            {
                "type": "tool.execution_complete",
                "timestamp": "2026-10-09T07:09:47.487Z",
                "data": {
                    "toolCallId": "call_yOch1fsPKzS4tjqeN3LGNlYw",
                    "success": False,
                    "error": {"message": "Path does not exist", "code": "failure"},
                },
            }
        ]
        results = [m for m in _normalize_copilot(records) if m.role == "tool_result"]
        assert len(results) == 1
        assert results[0].is_error is True
        assert results[0].tool_result == "Path does not exist"
        assert results[0].content == "Path does not exist"


# ---------------------------------------------------------------------------
# Integration tests for read_transcript
# ---------------------------------------------------------------------------


class TestReadTranscript:
    def test_gptme_transcript(self, gptme_jsonl: Path):
        t = read_transcript(gptme_jsonl)
        assert t.harness == "gptme"
        assert t.schema_version == TRANSCRIPT_SCHEMA_VERSION
        assert len(t.messages) == 3
        assert "view_transcript" in t.capabilities

    def test_cc_transcript(self, cc_jsonl: Path):
        t = read_transcript(cc_jsonl)
        assert t.harness == "claude-code"
        assert t.model == "claude-opus-4-6"
        assert len(t.messages) > 0
        assert "view_transcript" in t.capabilities

    def test_codex_transcript(self, codex_jsonl: Path):
        t = read_transcript(codex_jsonl)
        assert t.harness == "codex"
        assert t.model == "gpt-5.3-codex"
        assert len(t.messages) > 0

    def test_copilot_transcript(self, copilot_jsonl: Path):
        t = read_transcript(copilot_jsonl)
        assert t.harness == "copilot"
        assert t.model == "gpt-5.4"
        assert len(t.messages) > 0

    def test_grok_transcript(self, grok_jsonl: Path):
        t = read_transcript(grok_jsonl)
        assert t.harness == "grok"
        assert t.model == "grok-4.5-build"
        assert len(t.messages) > 0
        tool_msgs = [m for m in t.messages if m.tool_name]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].tool_name == "write"

    def test_started_at_and_last_activity(self, gptme_jsonl: Path):
        t = read_transcript(gptme_jsonl)
        assert t.started_at is not None
        assert t.last_activity is not None
        assert t.started_at <= t.last_activity

    def test_trajectory_path_is_absolute(self, gptme_jsonl: Path):
        t = read_transcript(gptme_jsonl)
        assert Path(t.trajectory_path).is_absolute()

    def test_to_json_roundtrip(self, gptme_jsonl: Path):
        t = read_transcript(gptme_jsonl)
        data = json.loads(t.to_json())
        assert data["schema_version"] == TRANSCRIPT_SCHEMA_VERSION
        assert data["harness"] == "gptme"
        assert isinstance(data["messages"], list)
        assert len(data["messages"]) == len(t.messages)

    def test_empty_file(self, tmp_path: Path):
        empty = tmp_path / "empty.jsonl"
        empty.write_text("")
        t = read_transcript(empty)
        assert t.messages == []
        assert "view_transcript" not in t.capabilities

    def test_message_to_dict_omits_none(self, gptme_jsonl: Path):
        t = read_transcript(gptme_jsonl)
        for msg in t.messages:
            d = msg.to_dict()
            # None fields should not appear
            assert "tool_name" not in d or d["tool_name"] is not None
            assert "tool_input" not in d or d["tool_input"] is not None

    def test_gptme_session_directory(self, gptme_jsonl: Path):
        """read_transcript resolves gptme session dirs to conversation.jsonl."""
        # gptme_jsonl is a file; move it into a dir as conversation.jsonl
        session_dir = gptme_jsonl.parent / "2026-04-07-test-session"
        session_dir.mkdir()
        dest = session_dir / "conversation.jsonl"
        dest.write_text(gptme_jsonl.read_text())
        t = read_transcript(session_dir)
        assert t.harness == "gptme"
        assert len(t.messages) == 3

    def test_directory_without_conversation_jsonl(self, tmp_path: Path):
        """read_transcript raises FileNotFoundError for dirs without conversation.jsonl."""
        empty_dir = tmp_path / "empty-session"
        empty_dir.mkdir()
        with pytest.raises(FileNotFoundError, match="conversation.jsonl"):
            read_transcript(empty_dir)


class TestNormalizedMessageSerialization:
    def test_to_dict_basic(self):
        msg = NormalizedMessage(role="user", content="hello")
        d = msg.to_dict()
        assert d == {"role": "user", "content": "hello"}

    def test_to_dict_with_tool(self):
        msg = NormalizedMessage(
            role="assistant",
            content="",
            tool_name="Bash",
            tool_input={"command": "ls"},
        )
        d = msg.to_dict()
        assert d["tool_name"] == "Bash"
        assert d["tool_input"] == {"command": "ls"}
        # content="" should be omitted? No — content is always present.
        # Check is_error=False is omitted
        assert "is_error" not in d

    def test_to_dict_error_flag_included(self):
        msg = NormalizedMessage(role="tool_result", content="fail", is_error=True)
        d = msg.to_dict()
        assert d["is_error"] is True


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestTranscriptCli:
    def test_transcript_json(self, gptme_jsonl: Path):
        from click.testing import CliRunner
        from gptme_sessions.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["transcript", str(gptme_jsonl), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["schema_version"] == TRANSCRIPT_SCHEMA_VERSION
        assert data["harness"] == "gptme"
        assert isinstance(data["messages"], list)

    def test_transcript_messages_only(self, gptme_jsonl: Path):
        from click.testing import CliRunner
        from gptme_sessions.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["transcript", str(gptme_jsonl), "--messages-only"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert all("role" in m for m in data)

    def test_transcript_human_readable(self, gptme_jsonl: Path):
        from click.testing import CliRunner
        from gptme_sessions.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["transcript", str(gptme_jsonl)])
        assert result.exit_code == 0, result.output
        assert "gptme" in result.output
        assert "Messages:" in result.output
        assert "Last activity: " in result.output

    def test_transcript_missing_file(self, tmp_path: Path):
        from click.testing import CliRunner
        from gptme_sessions.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["transcript", str(tmp_path / "nonexistent.jsonl")])
        assert result.exit_code != 0

    def test_transcript_cc_json(self, cc_jsonl: Path):
        from click.testing import CliRunner
        from gptme_sessions.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["transcript", str(cc_jsonl), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["harness"] == "claude-code"
        assert data["model"] == "claude-opus-4-6"


# ---------------------------------------------------------------------------
# End-to-end: gptme session → ATIF export
# ---------------------------------------------------------------------------


class TestGptmeAtifExport:
    """Verify that a gptme session with tool use exports to ATIF with tool
    structure intact — tool_calls and linked observation.results."""

    @pytest.fixture
    def gptme_tool_session(self, tmp_path: Path) -> Path:
        """A minimal gptme conversation with one AT-format call and its result."""
        records = [
            {
                "role": "user",
                "content": "List the files.",
                "timestamp": "2026-03-01T10:00:00+00:00",
            },
            {
                "role": "assistant",
                "content": 'Listing files.\n\n@shell(call-abc123): {"command": "ls"}',
                "timestamp": "2026-03-01T10:01:00+00:00",
                "metadata": {"usage": {"input_tokens": 20, "output_tokens": 8}},
            },
            {
                "role": "system",
                "content": "file1.py\nfile2.py",
                "timestamp": "2026-03-01T10:01:05+00:00",
            },
        ]
        path = tmp_path / "conversation.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        return path

    def test_tool_calls_present_in_atif_step(self, gptme_tool_session: Path):
        transcript = read_transcript(gptme_tool_session)
        atif = session_transcript_to_atif(transcript)
        agent_steps = [s for s in atif["steps"] if s["source"] == "agent"]
        assert len(agent_steps) >= 1
        step_with_calls = next((s for s in agent_steps if s.get("tool_calls")), None)
        assert step_with_calls is not None, "Expected at least one ATIF step with tool_calls"

    def test_tool_call_function_name_and_arguments(self, gptme_tool_session: Path):
        transcript = read_transcript(gptme_tool_session)
        atif = session_transcript_to_atif(transcript)
        agent_step = next(s for s in atif["steps"] if s.get("tool_calls"))
        tc = agent_step["tool_calls"][0]
        assert tc["function_name"] == "shell"
        assert tc["arguments"] == {"command": "ls"}
        assert tc["tool_call_id"] == "call-abc123"

    def test_observation_results_present(self, gptme_tool_session: Path):
        transcript = read_transcript(gptme_tool_session)
        atif = session_transcript_to_atif(transcript)
        agent_step = next(s for s in atif["steps"] if s.get("tool_calls"))
        assert "observation" in agent_step
        results = agent_step["observation"]["results"]
        assert len(results) >= 1

    def test_source_call_id_links_call_to_result(self, gptme_tool_session: Path):
        """source_call_id in observation must match the tool_call_id on the call."""
        transcript = read_transcript(gptme_tool_session)
        atif = session_transcript_to_atif(transcript)
        agent_step = next(s for s in atif["steps"] if s.get("tool_calls"))
        tc_id = agent_step["tool_calls"][0]["tool_call_id"]
        result = agent_step["observation"]["results"][0]
        assert result["source_call_id"] == tc_id == "call-abc123"
        assert "file1.py" in result["content"]

    def test_metrics_populated_from_usage(self, gptme_tool_session: Path):
        transcript = read_transcript(gptme_tool_session)
        atif = session_transcript_to_atif(transcript)
        agent_step = next(s for s in atif["steps"] if s.get("tool_calls"))
        assert agent_step.get("metrics", {}).get("input_tokens") == 20
        assert agent_step.get("metrics", {}).get("output_tokens") == 8

    def test_zero_tool_calls_before_fix_is_now_nonzero(self, gptme_tool_session: Path):
        """Regression: before this fix the step had 0 tool_calls."""
        transcript = read_transcript(gptme_tool_session)
        atif = session_transcript_to_atif(transcript)
        total_tool_calls = sum(len(s.get("tool_calls", [])) for s in atif["steps"])
        assert total_tool_calls > 0

    def test_zero_observations_before_fix_is_now_nonzero(self, gptme_tool_session: Path):
        """Regression: before this fix there were 0 observation blocks."""
        transcript = read_transcript(gptme_tool_session)
        atif = session_transcript_to_atif(transcript)
        total_observations = sum(
            len(s.get("observation", {}).get("results", [])) for s in atif["steps"]
        )
        assert total_observations > 0
