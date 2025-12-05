"""Tests for ProjectMonitoringRun class."""

import json
from unittest.mock import MagicMock, patch

import pytest

from run_loops.project_monitoring import ProjectMonitoringRun, WorkItem


@pytest.fixture
def workspace(tmp_path):
    """Create a temporary workspace."""
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()

    # Create logs directory for state tracking
    logs_dir = workspace_dir / "logs"
    logs_dir.mkdir()

    return workspace_dir


def test_project_monitoring_init(workspace):
    """Test ProjectMonitoringRun initialization."""
    run = ProjectMonitoringRun(workspace)

    assert run.workspace == workspace
    assert run.run_type == "project-monitoring"
    assert run.timeout == 1800  # 30 minutes
    assert run.lock_wait is False
    assert run.target_org == "gptme"
    assert run.author == ""  # No default author
    assert run.agent_name == "Agent"  # Default agent name
    assert run.state_dir.exists()


def test_project_monitoring_custom_org(workspace):
    """Test ProjectMonitoringRun with custom organization."""
    run = ProjectMonitoringRun(
        workspace, target_org="custom-org", author="custom-author"
    )

    assert run.target_org == "custom-org"
    assert run.author == "custom-author"


@patch("run_loops.project_monitoring.subprocess.run")
def test_discover_repositories_success(mock_run, workspace):
    """Test successful repository discovery."""
    # Mock gh repo list response
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="gptme/gptme\ngptme/gptme-webui\ngptme/gptme-contrib\n",
        stderr="",
    )

    run = ProjectMonitoringRun(workspace)
    repos = run.discover_repositories()

    assert len(repos) == 3
    assert "gptme/gptme" in repos
    assert "gptme/gptme-webui" in repos


@patch("run_loops.project_monitoring.subprocess.run")
def test_discover_repositories_failure(mock_run, workspace):
    """Test repository discovery failure."""
    # Mock gh command failure
    mock_run.return_value = MagicMock(
        returncode=1,
        stdout="",
        stderr="gh: not authenticated",
    )

    run = ProjectMonitoringRun(workspace)
    repos = run.discover_repositories()

    assert repos == []


@patch("run_loops.project_monitoring.subprocess.run")
def test_should_post_comment_first_time(workspace):
    """Test posting comment for first time."""
    run = ProjectMonitoringRun(workspace)

    # Mock gh pr view to return updated time
    with patch("run_loops.project_monitoring.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="2025-11-25T10:00:00Z",
            stderr="",
        )

        # First time: should post
        should_post = run.should_post_comment("gptme/gptme", 123, "update")
        assert should_post is True

        # State file should be created
        state_file = run.state_dir / "gptme-gptme-pr-123-comment.state"
        assert state_file.exists()


def test_should_post_comment_duplicate(workspace):
    """Test spam prevention for duplicate comments."""
    run = ProjectMonitoringRun(workspace)

    # Create existing state file (comment posted 1 hour ago)
    from datetime import datetime, timedelta

    prev_time = (datetime.now() - timedelta(hours=1)).isoformat()
    state_file = run.state_dir / "gptme-gptme-pr-123-comment.state"
    state_file.write_text(f"update {prev_time} 2025-11-25T09:00:00Z")

    # Mock gh pr view to return same updated time (no changes)
    with patch("run_loops.project_monitoring.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="2025-11-25T09:00:00Z",
            stderr="",
        )

        # Should NOT post (duplicate)
        should_post = run.should_post_comment("gptme/gptme", 123, "update")
        assert should_post is False


def test_should_post_comment_pr_updated(workspace):
    """Test posting comment when PR is updated."""
    run = ProjectMonitoringRun(workspace)

    # Create existing state file
    from datetime import datetime, timedelta

    prev_time = (datetime.now() - timedelta(hours=1)).isoformat()
    state_file = run.state_dir / "gptme-gptme-pr-123-comment.state"
    state_file.write_text(f"update {prev_time} 2025-11-25T09:00:00Z")

    # Mock gh pr view to return newer updated time (PR has changes)
    with patch("run_loops.project_monitoring.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="2025-11-25T11:00:00Z",  # Newer than state file
            stderr="",
        )

        # Should post (PR updated)
        should_post = run.should_post_comment("gptme/gptme", 123, "update")
        assert should_post is True


def test_should_post_comment_type_changed(workspace):
    """Test posting comment when comment type changes."""
    run = ProjectMonitoringRun(workspace)

    # Create existing state file with "update" type
    from datetime import datetime, timedelta

    prev_time = (datetime.now() - timedelta(hours=1)).isoformat()
    state_file = run.state_dir / "gptme-gptme-pr-123-comment.state"
    state_file.write_text(f"update {prev_time} 2025-11-25T10:00:00Z")

    # Mock gh pr view
    with patch("run_loops.project_monitoring.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="2025-11-25T10:00:00Z",
            stderr="",
        )

        # Should post (type changed from "update" to "ci_failure")
        should_post = run.should_post_comment("gptme/gptme", 123, "ci_failure")
        assert should_post is True


def test_should_post_comment_stale(workspace):
    """Test posting comment when previous comment is stale (24+ hours)."""
    run = ProjectMonitoringRun(workspace)

    # Create existing state file (comment posted 25 hours ago)
    from datetime import datetime, timedelta

    prev_time = (datetime.now() - timedelta(hours=25)).isoformat()
    state_file = run.state_dir / "gptme-gptme-pr-123-comment.state"
    state_file.write_text(f"update {prev_time} 2025-11-25T10:00:00Z")

    # Mock gh pr view
    with patch("run_loops.project_monitoring.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="2025-11-25T10:00:00Z",
            stderr="",
        )

        # Should post (stale comment)
        should_post = run.should_post_comment("gptme/gptme", 123, "update")
        assert should_post is True


@patch("run_loops.project_monitoring.subprocess.run")
def test_check_pr_updates_new_pr(mock_run, workspace):
    """Test detecting new PR updates."""
    # Mock gh pr list response
    pr_data = json.dumps(
        [
            {
                "number": 123,
                "title": "Add new feature",
                "updatedAt": "2025-11-25T10:00:00Z",
                "url": "https://github.com/gptme/gptme/pull/123",
            }
        ]
    )
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=pr_data,
        stderr="",
    )

    run = ProjectMonitoringRun(workspace)
    work_items = run.check_pr_updates("gptme/gptme")

    assert len(work_items) == 1
    assert work_items[0].item_type == "pr_update"
    assert work_items[0].number == 123
    assert work_items[0].repo == "gptme/gptme"


@patch("run_loops.project_monitoring.subprocess.run")
def test_check_pr_updates_no_change(mock_run, workspace):
    """Test PR with no updates."""
    # Mock gh pr list response
    pr_data = json.dumps(
        [
            {
                "number": 123,
                "title": "Add new feature",
                "updatedAt": "2025-11-25T10:00:00Z",
                "url": "https://github.com/gptme/gptme/pull/123",
            }
        ]
    )
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=pr_data,
        stderr="",
    )

    run = ProjectMonitoringRun(workspace)

    # First check: should find work
    work_items = run.check_pr_updates("gptme/gptme")
    assert len(work_items) == 1

    # Second check with same timestamp: should find nothing
    work_items = run.check_pr_updates("gptme/gptme")
    assert len(work_items) == 0


@patch("run_loops.project_monitoring.subprocess.run")
def test_check_ci_failures(mock_run, workspace):
    """Test detecting CI failures."""
    # Mock gh pr list response with failing checks
    pr_data = json.dumps(
        [
            {
                "number": 123,
                "title": "Add new feature",
                "url": "https://github.com/gptme/gptme/pull/123",
                "statusCheckRollup": [
                    {"conclusion": "SUCCESS"},
                    {"conclusion": "FAILURE"},
                ],
            }
        ]
    )
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=pr_data,
        stderr="",
    )

    run = ProjectMonitoringRun(workspace)
    work_items = run.check_ci_failures("gptme/gptme")

    assert len(work_items) == 1
    assert work_items[0].item_type == "ci_failure"
    assert work_items[0].number == 123


@patch("run_loops.project_monitoring.subprocess.run")
def test_check_assigned_issues(mock_run, workspace):
    """Test detecting assigned issues."""
    # Mock gh issue list response
    issue_data = json.dumps(
        [
            {
                "number": 456,
                "title": "Fix bug",
                "url": "https://github.com/gptme/gptme/issues/456",
            }
        ]
    )
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=issue_data,
        stderr="",
    )

    run = ProjectMonitoringRun(workspace)
    work_items = run.check_assigned_issues("gptme/gptme")

    assert len(work_items) == 1
    assert work_items[0].item_type == "assigned_issue"
    assert work_items[0].number == 456


def test_generate_prompt_no_work(workspace):
    """Test prompt generation when no work found."""
    run = ProjectMonitoringRun(workspace)

    with patch.object(run, "discover_work", return_value=[]):
        prompt = run.generate_prompt()
        assert prompt == ""


def test_generate_prompt_with_work(workspace):
    """Test prompt generation with cached work items."""
    run = ProjectMonitoringRun(workspace)

    work_items = [
        WorkItem(
            repo="gptme/gptme",
            item_type="pr_update",
            number=123,
            title="Add feature",
            url="https://github.com/gptme/gptme/pull/123",
            details="PR #123 updated",
        )
    ]

    # Set cached work directly (simulates has_work() having been called)
    run._discovered_work = work_items
    prompt = run.generate_prompt()

    assert "gptme/gptme" in prompt
    assert "PR #123" in prompt
    assert "GREEN" in prompt
    assert "RED" in prompt


@patch("run_loops.base.execute_gptme")
def test_execute_with_work(mock_execute, workspace):
    """Test execute when work is found."""
    # Mock gptme execution - use ExecutionResult
    from run_loops.utils.execution import ExecutionResult

    mock_execute.return_value = ExecutionResult(exit_code=0, timed_out=False)

    # Create work items (simulates has_work() having been called)
    work_items = [
        WorkItem(
            repo="gptme/gptme",
            item_type="pr_update",
            number=123,
            title="Add feature",
            url="https://github.com/gptme/gptme/pull/123",
            details="PR #123 updated",
        )
    ]

    run = ProjectMonitoringRun(workspace)
    # Set cached work directly (simulates has_work() having been called)
    run._discovered_work = work_items

    prompt = run.generate_prompt()
    result = run.execute(prompt)

    assert result.exit_code == 0
    mock_execute.assert_called_once()


def test_execute_no_work(workspace):
    """Test execute when no work found."""
    run = ProjectMonitoringRun(workspace)

    with patch.object(run, "discover_work", return_value=[]):
        prompt = run.generate_prompt()
        result = run.execute(prompt)

        assert result.exit_code == 0
        assert result.success is True
