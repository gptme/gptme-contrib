"""Tests for the normalized session transcript module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gptme_sessions.transcript import (
    NormalizedMessage,
    TRANSCRIPT_SCHEMA_VERSION,
    _normalize_cc,
    _normalize_codex,
    _normalize_copilot,
    _normalize_gptme,
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


# ---------------------------------------------------------------------------
# Unit tests for normalizers
# ---------------------------------------------------------------------------


class TestNormalizeGptme:
    def test_basic_roles(self, gptme_jsonl: Path):
        records = [json.loads(line) for line in gptme_jsonl.read_text().strip().splitlines()]
        norm = _normalize_gptme(records)
        assert len(norm) == 3
        assert norm[0].role == "user"
        assert norm[1].role == "assistant"
        assert norm[2].role == "system"

    def test_content_preserved(self, gptme_jsonl: Path):
        records = [json.loads(line) for line in gptme_jsonl.read_text().strip().splitlines()]
        norm = _normalize_gptme(records)
        assert "Hello" in norm[0].content
        assert "test.py" in norm[1].content

    def test_timestamp_preserved(self, gptme_jsonl: Path):
        records = [json.loads(line) for line in gptme_jsonl.read_text().strip().splitlines()]
        norm = _normalize_gptme(records)
        assert norm[0].timestamp is not None
        assert "2026-03-01" in norm[0].timestamp


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
        assert len(t.messages) > 0

    def test_copilot_transcript(self, copilot_jsonl: Path):
        t = read_transcript(copilot_jsonl)
        assert t.harness == "copilot"
        assert len(t.messages) > 0

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
