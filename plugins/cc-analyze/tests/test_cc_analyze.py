"""Tests for cc_analyze plugin."""

import subprocess
from unittest.mock import MagicMock, patch


def test_check_claude_available():
    """Test Claude CLI availability check."""
    from gptme_cc_analyze.tools.cc_analyze import _check_claude_available

    # Test should work in environment with claude installed
    result = _check_claude_available()
    # Either True (claude installed) or False (not installed)
    assert isinstance(result, bool)


def test_analyze_without_claude():
    """Test analyze returns error when claude not available."""
    from gptme_cc_analyze.tools.cc_analyze import AnalysisResult, analyze

    with patch(
        "gptme_cc_analyze.tools.cc_analyze._check_claude_available", return_value=False
    ):
        result = analyze("test prompt")
        assert isinstance(result, AnalysisResult)
        assert "Error: Claude CLI not found" in result.output
        assert result.exit_code == 1


def test_analyze_sync_success():
    """Test successful synchronous analysis."""
    from gptme_cc_analyze.tools.cc_analyze import AnalysisResult, analyze

    mock_result = MagicMock()
    mock_result.stdout = "Analysis complete: No issues found"
    mock_result.stderr = ""
    mock_result.returncode = 0

    with patch(
        "gptme_cc_analyze.tools.cc_analyze._check_claude_available", return_value=True
    ):
        with patch("subprocess.run", return_value=mock_result):
            result = analyze("test prompt", timeout=60)

            assert isinstance(result, AnalysisResult)
            assert "Analysis complete" in result.output
            assert result.exit_code == 0


def test_analyze_timeout():
    """Test analysis timeout handling."""
    from gptme_cc_analyze.tools.cc_analyze import AnalysisResult, analyze

    with patch(
        "gptme_cc_analyze.tools.cc_analyze._check_claude_available", return_value=True
    ):
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=10),
        ):
            result = analyze("test prompt", timeout=10)
            assert isinstance(result, AnalysisResult)
            assert "timed out" in result.output
            assert result.exit_code == -1


def test_analyze_background():
    """Test background analysis returns session ID."""
    from gptme_cc_analyze.tools.cc_analyze import analyze

    with patch(
        "gptme_cc_analyze.tools.cc_analyze._check_claude_available", return_value=True
    ):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = analyze("test prompt", background=True)

            assert isinstance(result, str)
            assert "Background analysis started" in result
            assert "cc_analyze_" in result


def test_check_session():
    """Test checking session status."""
    from gptme_cc_analyze.tools.cc_analyze import check_session

    # Test non-existent session
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1)
        result = check_session("nonexistent_session")
        assert "not found" in result


def test_kill_session():
    """Test killing session."""
    from gptme_cc_analyze.tools.cc_analyze import kill_session

    # Test successful kill
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = kill_session("test_session")
        assert "killed" in result

    # Test session not found
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "tmux")
        result = kill_session("nonexistent")
        assert "not found" in result


def test_tool_spec_exists():
    """Test that tool spec is properly defined."""
    from gptme_cc_analyze.tools.cc_analyze import tool

    assert tool.name == "cc_analyze"
    assert tool.block_types is not None
    assert "cc_analyze" in tool.block_types
    assert tool.functions is not None
    assert len(tool.functions) == 3  # analyze, check_session, kill_session
