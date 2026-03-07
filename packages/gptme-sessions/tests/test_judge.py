"""Tests for the LLM-as-judge module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch


from gptme_sessions.judge import (
    DEFAULT_GOALS,
    DEFAULT_JUDGE_MODEL,
    JUDGE_PROMPT_TEMPLATE,
    JUDGE_SYSTEM,
    judge_from_signals,
    judge_session,
)
from gptme_sessions.record import SessionRecord


class TestJudgeSession:
    """Tests for judge_session()."""

    def test_returns_none_without_anthropic(self) -> None:
        """Judge returns None when anthropic is not installed."""
        # Setting sys.modules["anthropic"] = None causes `import anthropic` to raise ImportError
        with patch.dict("sys.modules", {"anthropic": None}):
            result = judge_session("test session text", category="code", api_key="fake-key")
        assert result is None

    def test_returns_none_without_api_key(self) -> None:
        """Judge returns None when no API key is available."""
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("gptme_sessions.judge._get_api_key", return_value=""),
        ):
            result = judge_session("test text", category="code")
            assert result is None

    def test_successful_evaluation(self) -> None:
        """Judge returns score and reason on success."""
        # Create a mock anthropic module
        mock_anthropic = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"score": 0.75, "reason": "Good work"}')]
        mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response

        # patch.dict replaces sys.modules["anthropic"] so the `import anthropic`
        # inside judge_session picks up the mock without needing a module reload.
        with (
            patch.dict("sys.modules", {"anthropic": mock_anthropic}),
            patch("gptme_sessions.judge._get_api_key", return_value="test-key"),
        ):
            result = judge_session("session text", category="code")

        assert result is not None
        assert result["score"] == 0.75
        assert result["reason"] == "Good work"
        assert result["model"] == DEFAULT_JUDGE_MODEL

    def test_prompt_template_wellformed(self) -> None:
        """Verify that the prompt template and system prompt are well-formed."""
        prompt = JUDGE_PROMPT_TEMPLATE.format(
            goals="Test goals",
            category="code",
            journal="Did some work",
        )
        assert "Test goals" in prompt
        assert "code" in prompt
        assert "Did some work" in prompt
        assert "0.0-1.0" in prompt
        assert "JSON" in JUDGE_SYSTEM

    def test_score_clamping(self) -> None:
        """Out-of-range scores from the LLM are clamped to [0.0, 1.0]."""
        mock_anthropic = MagicMock()
        mock_response = MagicMock()
        # LLM returns score above 1.0
        mock_response.content = [MagicMock(text='{"score": 1.5, "reason": "Overconfident LLM"}')]
        mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response

        with (
            patch.dict("sys.modules", {"anthropic": mock_anthropic}),
            patch("gptme_sessions.judge._get_api_key", return_value="test-key"),
        ):
            result = judge_session("session text")

        assert result is not None
        assert result["score"] == 1.0  # clamped from 1.5

        # Also test clamping from below
        mock_response.content = [MagicMock(text='{"score": -0.3, "reason": "Below zero"}')]
        with (
            patch.dict("sys.modules", {"anthropic": mock_anthropic}),
            patch("gptme_sessions.judge._get_api_key", return_value="test-key"),
        ):
            result = judge_session("session text")

        assert result is not None
        assert result["score"] == 0.0  # clamped from -0.3

    def test_curly_braces_in_journal_dont_crash(self) -> None:
        """Journal text with curly braces (JSON, Python dicts) doesn't raise KeyError.

        str.format() only parses {placeholder} in the template itself — keyword
        argument values are substituted verbatim, so {/} in the journal are safe.
        """
        mock_anthropic = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"score": 0.6, "reason": "Wrote code"}')]
        mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response

        curly_brace_journal = (
            'Wrote code: {"key": "value", "nested": {"a": 1}}\n'
            "Shell expansion: ${HOME}/path\n"
            "Dict literal: {'foo': 'bar'}"
        )

        with (
            patch.dict("sys.modules", {"anthropic": mock_anthropic}),
            patch("gptme_sessions.judge._get_api_key", return_value="test-key"),
        ):
            result = judge_session(curly_brace_journal, category="code")

        assert result is not None
        assert result["score"] == 0.6

    def test_default_goals_is_generic(self) -> None:
        """Default goals should work for any agent, not just Bob."""
        assert "Bob" not in DEFAULT_GOALS
        assert "agent" in DEFAULT_GOALS.lower()


class TestJudgeFromSignals:
    """Tests for judge_from_signals()."""

    def test_synthesizes_summary_from_signals(self) -> None:
        """When no journal text is provided, a summary is built from signals."""
        signals: dict = {
            "git_commits": ["feat: add feature (abc1234)"],
            "file_writes": ["/path/to/file.py"],
            "tool_calls": {"Bash": 5, "Edit": 3},
            "grade": 0.70,
            "error_count": 1,
            "session_duration_s": 300,
        }

        # Mock judge_session to capture the synthesized text
        with patch("gptme_sessions.judge.judge_session") as mock_judge:
            mock_judge.return_value = {"score": 0.65, "reason": "Test", "model": "haiku"}
            judge_from_signals(signals, category="code")

            # Verify judge_session was called with synthesized text
            mock_judge.assert_called_once()
            call_args = mock_judge.call_args
            journal_text = call_args[0][0]  # first positional arg
            assert "abc1234" in journal_text
            assert "/path/to/file.py" in journal_text
            assert "Bash:5" in journal_text
            assert "0.70" in journal_text

    def test_uses_provided_journal_text(self) -> None:
        """When journal text is provided, it's used directly."""
        signals: dict = {"git_commits": [], "file_writes": [], "tool_calls": {}}

        with patch("gptme_sessions.judge.judge_session") as mock_judge:
            mock_judge.return_value = {"score": 0.50, "reason": "Test", "model": "haiku"}
            judge_from_signals(signals, journal_text="My custom journal", category="triage")

            call_args = mock_judge.call_args
            assert call_args[0][0] == "My custom journal"

    def test_forwards_kwargs(self) -> None:
        """Extra kwargs are forwarded to judge_session."""
        signals: dict = {"git_commits": [], "file_writes": [], "tool_calls": {}}

        with patch("gptme_sessions.judge.judge_session") as mock_judge:
            mock_judge.return_value = {"score": 0.50, "reason": "Test", "model": "haiku"}
            judge_from_signals(
                signals,
                journal_text="text",
                category="code",
                goals="Custom goals",
                model="custom-model",
            )

            call_args = mock_judge.call_args
            assert call_args[1]["goals"] == "Custom goals"
            assert call_args[1]["model"] == "custom-model"


class TestSessionRecordJudgeFields:
    """Tests for LLM judge fields on SessionRecord."""

    def test_judge_fields_default_to_none(self) -> None:
        """New records have judge fields as None by default."""
        record = SessionRecord(session_id="test")
        assert record.llm_judge_score is None
        assert record.llm_judge_reason is None
        assert record.llm_judge_model is None

    def test_judge_fields_serialization(self) -> None:
        """Judge fields survive round-trip serialization."""
        record = SessionRecord(
            session_id="test",
            llm_judge_score=0.75,
            llm_judge_reason="Good strategic work",
            llm_judge_model="claude-haiku-4-5-20251001",
        )
        d = record.to_dict()
        assert d["llm_judge_score"] == 0.75
        assert d["llm_judge_reason"] == "Good strategic work"
        assert d["llm_judge_model"] == "claude-haiku-4-5-20251001"

        # Round-trip via JSON
        json_str = record.to_json()
        restored = SessionRecord.from_dict(json.loads(json_str))
        assert restored.llm_judge_score == 0.75
        assert restored.llm_judge_reason == "Good strategic work"
        assert restored.llm_judge_model == "claude-haiku-4-5-20251001"

    def test_judge_fields_ignored_in_old_records(self) -> None:
        """Old records without judge fields load cleanly."""
        old_data = {
            "session_id": "abc123",
            "timestamp": "2026-03-07T12:00:00+00:00",
            "outcome": "productive",
        }
        record = SessionRecord.from_dict(old_data)
        assert record.llm_judge_score is None


class TestJudgeCLI:
    """Tests for the judge CLI command."""

    def test_judge_command_exists(self) -> None:
        """The 'judge' command is registered in the CLI group."""
        from gptme_sessions.cli import cli

        assert "judge" in cli.commands

    def test_signals_has_llm_judge_flag(self) -> None:
        """The 'signals' command has --llm-judge flag."""
        from gptme_sessions.cli import cli

        signals_cmd = cli.commands["signals"]
        param_names = [p.name for p in signals_cmd.params]
        assert "llm_judge" in param_names
        assert "goals" in param_names

    def test_judge_skips_unreadable_files(self, tmp_path: "Path") -> None:
        """A single unreadable journal file is skipped; other entries are processed."""
        from click.testing import CliRunner
        from gptme_sessions.cli import cli

        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()

        # Good entry — must match the glob "autonomous-session-*.md"
        good_day = journal_dir / "2026-03-07"
        good_day.mkdir()
        good_entry = good_day / "autonomous-session-abc123.md"
        good_entry.write_text("## Session\nDid some work", encoding="utf-8")

        # Bad entry — unreadable (no read permission)
        bad_day = journal_dir / "2026-03-06"
        bad_day.mkdir()
        bad_entry = bad_day / "autonomous-session-def456.md"
        bad_entry.write_text("corrupt", encoding="utf-8")
        bad_entry.chmod(0o000)

        runner = CliRunner()
        try:
            result = runner.invoke(
                cli,
                ["judge", "--journal-dir", str(journal_dir), "--dry-run"],
            )
            # Should not crash; good entry should be processed, bad skipped
            assert result.exit_code == 0, result.output
            # Good entry was found (not "No autonomous session journal entries found")
            assert "2026-03-07" in result.output or "abc123" in result.output
        finally:
            bad_entry.chmod(0o644)  # restore so tmp_path cleanup works
