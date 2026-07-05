"""Tests for Claude Code plugin."""

from unittest.mock import MagicMock, patch

import pytest
from gptme_claude_code.tools.claude_code import (
    ClaudeCodeResult,
    _check_claude_available,
    analyze,
    ask,
    check_session,
    fix,
    implement,
    kill_session,
)


class TestClaudeAvailability:
    """Test claude CLI availability checking."""

    def test_check_available_when_installed(self):
        """Test detection when claude is installed."""
        with patch("shutil.which", return_value="/usr/bin/claude"):
            assert _check_claude_available() is True

    def test_check_unavailable_when_not_installed(self):
        """Test detection when claude is not installed."""
        with patch("shutil.which", return_value=None):
            assert _check_claude_available() is False


class TestAnalyze:
    """Test analyze function."""

    def test_returns_error_when_claude_unavailable(self):
        """Test error handling when claude CLI is not installed."""
        with patch("shutil.which", return_value=None):
            result = analyze("test prompt", timeout=60)
            assert isinstance(result, ClaudeCodeResult)
            assert "Error: Claude CLI not found" in result.output
            assert result.exit_code == 1

    def test_raises_on_long_sync_timeout(self):
        """Test that long sync timeouts raise ValueError."""
        with pytest.raises(ValueError, match="destroying prompt cache"):
            analyze("test", timeout=300, background=False)

    def test_allows_background_long_timeout(self):
        """Test that background mode allows long timeouts."""
        with patch("shutil.which", return_value="/usr/bin/claude"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                result = analyze("test", timeout=1800, background=True)
                assert "Background" in result


class TestAsk:
    """Test ask function."""

    def test_formats_question_properly(self):
        """Test that questions are formatted into prompts."""
        with patch("shutil.which", return_value="/usr/bin/claude"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="Answer here", stderr=""
                )
                ask("How does auth work?")
                # Check the prompt was constructed
                call_args = mock_run.call_args
                cmd = call_args[0][0]
                assert "claude" in cmd
                assert "-p" in cmd


class TestFix:
    """Test fix function."""

    def test_includes_no_commit_instruction_by_default(self):
        """Test that auto_commit=False adds no-commit instruction."""
        with patch("shutil.which", return_value="/usr/bin/claude"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="Fixed", stderr=""
                )
                fix("Fix type errors", auto_commit=False)
                call_args = mock_run.call_args
                cmd = call_args[0][0]
                prompt = cmd[cmd.index("-p") + 1]
                assert "Do NOT commit" in prompt


class TestImplement:
    """Test implement function."""

    def test_includes_worktree_instructions_when_requested(self):
        """Test that use_worktree=True adds worktree instructions."""
        with patch("shutil.which", return_value="/usr/bin/claude"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="Implemented", stderr=""
                )
                implement("Add feature", use_worktree=True, branch_name="test-branch")
                call_args = mock_run.call_args
                cmd = call_args[0][0]
                prompt = cmd[cmd.index("-p") + 1]
                assert "worktree" in prompt
                assert "test-branch" in prompt


class TestSessionManagement:
    """Test background session management."""

    def test_check_session_returns_output(self):
        """Test checking a running session."""
        with patch("subprocess.run") as mock_run:
            # First call: has-session succeeds
            # Second call: capture-pane returns output
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout="Session output", stderr=""),
            ]
            result = check_session("test_session")
            assert result == "Session output"

    def test_check_session_not_found(self):
        """Test checking a non-existent session."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = check_session("nonexistent")
            assert "not found" in result

    def test_kill_session_success(self):
        """Test killing a session."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = kill_session("test_session")
            assert "killed" in result

    def test_kill_session_not_found(self):
        """Test killing a non-existent session."""
        with patch("subprocess.run") as mock_run:
            from subprocess import CalledProcessError

            mock_run.side_effect = CalledProcessError(1, "tmux")
            result = kill_session("nonexistent")
            assert "not found" in result
