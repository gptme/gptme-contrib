"""Tests for EmailRun class."""

from unittest.mock import MagicMock, patch

import pytest

from run_loops.email import EmailRun


@pytest.fixture
def workspace(tmp_path):
    """Create a temporary workspace."""
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()

    # Create email directory structure (gptmail package)
    email_dir = workspace_dir / "gptme-contrib/packages/gptmail"
    email_dir.mkdir(parents=True)

    return workspace_dir


def test_email_run_init(workspace):
    """Test EmailRun initialization."""
    run = EmailRun(workspace)

    assert run.workspace == workspace
    assert run.run_type == "email"
    assert run.timeout == 1200  # 20 minutes
    assert run.lock_wait is False


@patch("run_loops.base.git_pull_with_retry")
def test_pre_run_success(mock_git_pull, workspace):
    """Test pre_run performs git pull (email sync moved to has_work)."""
    mock_git_pull.return_value = True

    run = EmailRun(workspace)
    result = run.pre_run()

    # pre_run just does git pull now (email sync is in has_work)
    assert result is True
    mock_git_pull.assert_called_once()


@patch("run_loops.email.subprocess.run")
def test_has_work_no_emails(mock_run, workspace):
    """Test has_work returns False when no unreplied emails."""
    # Mock responses: mbsync success, gptmail sync success, check-unreplied returns 0 (no emails)
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

    run = EmailRun(workspace)
    result = run.has_work()

    assert result is False
    # Should have called mbsync, gptmail sync, and check-unreplied
    assert mock_run.call_count >= 3


@patch("run_loops.email.subprocess.run")
def test_has_work_with_emails(mock_run, workspace):
    """Test has_work returns True when unreplied emails exist."""

    # Mock responses: mbsync success, gptmail sync success, check-unreplied returns 1 (has emails)
    def side_effect(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if "check-unreplied" in str(cmd):
            return MagicMock(
                returncode=1, stdout="email1@example.com: Subject line", stderr=""
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    mock_run.side_effect = side_effect

    run = EmailRun(workspace)
    result = run.has_work()

    assert result is True
    assert run._work_description is not None


@patch("run_loops.email.subprocess.run")
def test_has_work_mbsync_failure(mock_run, workspace):
    """Test has_work continues checking even if mbsync fails."""

    # Mock mbsync failure but check-unreplied returns 0 (no emails)
    def side_effect(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if "mbsync" in str(cmd):
            return MagicMock(returncode=1, stderr="Connection failed")
        return MagicMock(returncode=0, stdout="", stderr="")

    mock_run.side_effect = side_effect

    run = EmailRun(workspace)
    result = run.has_work()

    # Should still complete check (returns False since no emails)
    assert result is False


@patch("run_loops.base.execute_gptme")
def test_execute_runs_gptme(mock_execute, workspace):
    """Test execute calls gptme (has_work already confirmed emails exist)."""
    from run_loops.utils.execution import ExecutionResult

    mock_execute.return_value = ExecutionResult(exit_code=0, timed_out=False)

    run = EmailRun(workspace)
    prompt = "Test prompt"
    result = run.execute(prompt)

    # execute just runs gptme (has_work already confirmed there's work)
    mock_execute.assert_called_once()
    assert result.exit_code == 0
    assert result.success is True


def test_generate_prompt(workspace):
    """Test prompt generation."""
    run = EmailRun(workspace)
    prompt = run.generate_prompt()

    assert "email" in prompt.lower()
    assert "check-unreplied" in prompt
    assert "gptmail" in prompt
