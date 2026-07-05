"""Tests for the replay surface."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from gptme_sessions.record import SessionRecord
from gptme_sessions.store import SessionStore


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    return path


@pytest.fixture
def codex_replay_jsonl(tmp_path: Path) -> Path:
    records = [
        {
            "type": "session_meta",
            "timestamp": "2026-03-01T09:59:59.000Z",
            "payload": {"originator": "codex_exec", "model": "gpt-5.4"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-03-01T10:00:00.000Z",
            "payload": {
                "type": "message",
                "role": "system",
                "content": [{"type": "text", "text": "System prelude that should collapse."}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-03-01T10:00:01.000Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "text", "text": "Show me the files."}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-03-01T10:00:02.000Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Listing files now."}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-03-01T10:00:03.000Z",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "ls", "yield_time_ms": 1000}),
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-03-01T10:00:04.000Z",
            "payload": {
                "type": "function_call_output",
                "output": "line1\nline2\nline3\nline4\nline5\nline6",
            },
        },
    ]
    return _write_jsonl(tmp_path / "rollout.jsonl", records)


class TestReplayCli:
    def test_replay_path_collapses_initial_system_prelude(self, codex_replay_jsonl: Path):
        from click.testing import CliRunner
        from gptme_sessions.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["replay", str(codex_replay_jsonl)])
        assert result.exit_code == 0, result.output
        assert "Messages:      4 displayed (5 total)" in result.output
        assert "[system prelude collapsed: 1 message" in result.output
        assert "System prelude that should collapse." not in result.output
        assert "TOOL CALL  exec_command" in result.output
        assert "TOOL RESULT" in result.output
        assert "Model:         gpt-5.4" in result.output

    def test_replay_raw_system_shows_prelude(self, codex_replay_jsonl: Path):
        from click.testing import CliRunner
        from gptme_sessions.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["replay", str(codex_replay_jsonl), "--raw-system"])
        assert result.exit_code == 0, result.output
        assert "[system prelude collapsed:" not in result.output
        assert "SYSTEM" in result.output
        assert "System prelude that should collapse." in result.output

    def test_replay_tool_input_and_full_results(self, codex_replay_jsonl: Path):
        from click.testing import CliRunner
        from gptme_sessions.cli import cli

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "replay",
                str(codex_replay_jsonl),
                "--tool-input",
                "--tool-results",
                "full",
            ],
        )
        assert result.exit_code == 0, result.output
        assert '"cmd": "ls"' in result.output
        assert "line6" in result.output

    def test_replay_tail_renders_only_last_normalized_messages(self, codex_replay_jsonl: Path):
        from click.testing import CliRunner
        from gptme_sessions.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["replay", str(codex_replay_jsonl), "--tail", "2"])
        assert result.exit_code == 0, result.output
        assert "Messages:      2 displayed (5 total)" in result.output
        assert "TOOL CALL  exec_command" in result.output
        assert "TOOL RESULT" in result.output
        assert "Show me the files." not in result.output
        assert "Listing files now." not in result.output

    def test_replay_resolves_session_id_prefix(self, tmp_path: Path, codex_replay_jsonl: Path):
        from click.testing import CliRunner
        from gptme_sessions.cli import cli

        sessions_dir = tmp_path / "sessions"
        store = SessionStore(sessions_dir=sessions_dir)
        store.append(
            SessionRecord(
                session_id="abcd1234",
                harness="codex",
                trajectory_path=str(codex_replay_jsonl),
            )
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["--sessions-dir", str(sessions_dir), "replay", "abcd"])
        assert result.exit_code == 0, result.output
        assert "Harness:       codex" in result.output
        assert f"Source:        {codex_replay_jsonl.resolve()}" in result.output

    def test_replay_missing_trajectory_path_fails_cleanly(self, tmp_path: Path):
        from click.testing import CliRunner
        from gptme_sessions.cli import cli

        sessions_dir = tmp_path / "sessions"
        store = SessionStore(sessions_dir=sessions_dir)
        store.append(SessionRecord(session_id="abcd1234", harness="codex"))

        runner = CliRunner()
        result = runner.invoke(cli, ["--sessions-dir", str(sessions_dir), "replay", "abcd"])
        assert result.exit_code != 0
        assert "has no trajectory_path" in result.output


def test_replay_summary_uses_inline_ellipsis_for_single_line_char_truncation():
    from gptme_sessions.replay import _summarize_text

    summary = _summarize_text("x" * 400, max_chars=20)

    assert summary.endswith("...")
    assert "\n..." not in summary


def test_replay_logs_when_existing_path_wins_over_session_id_prefix(
    tmp_path: Path, codex_replay_jsonl: Path, monkeypatch: pytest.MonkeyPatch, caplog
):
    from gptme_sessions.replay import resolve_replay_target

    shadow_path = tmp_path / "abcd1234"
    shadow_path.write_text(codex_replay_jsonl.read_text())

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir)
    store.append(
        SessionRecord(
            session_id="abcd1234-session",
            harness="codex",
            trajectory_path=str(codex_replay_jsonl),
        )
    )

    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="gptme_sessions.replay"):
        transcript = resolve_replay_target("abcd1234", sessions_dir=sessions_dir)

    assert transcript.trajectory_path == str(shadow_path.resolve())
    assert "skipping session ID lookup" in caplog.text
