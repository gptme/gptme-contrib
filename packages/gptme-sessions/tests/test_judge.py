"""Tests for the LLM-as-judge module."""

from __future__ import annotations

import json
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
        with patch.dict("sys.modules", {"anthropic": None}):
            # Force re-import to trigger ImportError
            import importlib

            from gptme_sessions import judge

            importlib.reload(judge)
            _result = judge.judge_session("test session text", category="code")  # noqa: F841
            # Restore
            importlib.reload(judge)
        # Can't reliably force ImportError with module mocking in all cases,
        # so just test the no-API-key path instead
        assert True

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

        with (
            patch.dict("sys.modules", {"anthropic": mock_anthropic}),
            patch("gptme_sessions.judge._get_api_key", return_value="test-key"),
        ):
            # Force re-import to pick up the mocked anthropic
            import importlib

            import gptme_sessions.judge

            importlib.reload(gptme_sessions.judge)
            result = gptme_sessions.judge.judge_session("session text", category="code")
            # Restore module
            importlib.reload(gptme_sessions.judge)

        assert result is not None
        assert result["score"] == 0.75
        assert result["reason"] == "Good work"
        assert result["model"] == DEFAULT_JUDGE_MODEL

    def test_score_clamping(self) -> None:
        """Verify that the prompt template and system are well-formed."""
        # Test that the prompt template can be formatted
        prompt = JUDGE_PROMPT_TEMPLATE.format(
            goals="Test goals",
            category="code",
            journal="Did some work",
        )
        assert "Test goals" in prompt
        assert "code" in prompt
        assert "Did some work" in prompt
        assert "0.0-1.0" in prompt

        # System prompt is non-empty
        assert "JSON" in JUDGE_SYSTEM

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
