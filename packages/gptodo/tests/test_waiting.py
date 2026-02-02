"""Tests for gptodo waiting module."""

import pytest
from unittest.mock import patch, MagicMock
from gptodo.waiting import (
    WaitCondition,
    WaitType,
    parse_waiting_for,
    parse_github_ref,
    check_pr_ci,
    check_pr_merged,
    check_condition,
)


class TestWaitCondition:
    """Test WaitCondition parsing."""

    def test_from_string_simple(self):
        """Test parsing simple task dependency."""
        cond = WaitCondition.from_string("other-task")
        assert cond.type == WaitType.TASK
        assert cond.ref == "other-task"

    def test_from_string_github_url(self):
        """Test parsing GitHub URL as task dependency."""
        cond = WaitCondition.from_string("https://github.com/owner/repo/pull/123")
        assert cond.type == WaitType.TASK
        assert "github.com" in cond.ref

    def test_from_dict_pr_ci(self):
        """Test parsing structured pr_ci condition."""
        data = {"type": "pr_ci", "ref": "gptme/gptme#1217"}
        cond = WaitCondition.from_dict(data)
        assert cond.type == WaitType.PR_CI
        assert cond.ref == "gptme/gptme#1217"

    def test_from_dict_comment_with_pattern(self):
        """Test parsing comment condition with pattern."""
        data = {"type": "comment", "ref": "owner/repo#123", "pattern": "LGTM"}
        cond = WaitCondition.from_dict(data)
        assert cond.type == WaitType.COMMENT
        assert cond.pattern == "LGTM"


class TestParseWaitingFor:
    """Test parse_waiting_for function."""

    def test_empty_metadata(self):
        """Test empty waiting_for."""
        assert parse_waiting_for({}) == []
        assert parse_waiting_for({"waiting_for": None}) == []

    def test_string_format(self):
        """Test legacy string format."""
        conditions = parse_waiting_for({"waiting_for": "other-task"})
        assert len(conditions) == 1
        assert conditions[0].type == WaitType.TASK

    def test_dict_format(self):
        """Test single structured condition."""
        metadata = {"waiting_for": {"type": "pr_ci", "ref": "gptme/gptme#1217"}}
        conditions = parse_waiting_for(metadata)
        assert len(conditions) == 1
        assert conditions[0].type == WaitType.PR_CI

    def test_list_format(self):
        """Test list of conditions."""
        metadata = {
            "waiting_for": [
                {"type": "pr_ci", "ref": "gptme/gptme#1217"},
                {"type": "pr_merged", "ref": "gptme/gptme#1216"},
            ]
        }
        conditions = parse_waiting_for(metadata)
        assert len(conditions) == 2
        assert conditions[0].type == WaitType.PR_CI
        assert conditions[1].type == WaitType.PR_MERGED


class TestParseGitHubRef:
    """Test GitHub reference parsing."""

    def test_full_url_pr(self):
        """Test full PR URL."""
        owner, repo, num = parse_github_ref("https://github.com/gptme/gptme/pull/1217")
        assert owner == "gptme"
        assert repo == "gptme"
        assert num == 1217

    def test_full_url_issue(self):
        """Test full issue URL."""
        owner, repo, num = parse_github_ref("https://github.com/owner/repo/issues/123")
        assert owner == "owner"
        assert repo == "repo"
        assert num == 123

    def test_short_form(self):
        """Test short form owner/repo#123."""
        owner, repo, num = parse_github_ref("gptme/gptme#1217")
        assert owner == "gptme"
        assert repo == "gptme"
        assert num == 1217

    def test_invalid_format(self):
        """Test invalid format raises ValueError."""
        with pytest.raises(ValueError):
            parse_github_ref("just-a-task-name")


class TestCheckPrCi:
    """Test PR CI checking."""

    @patch("subprocess.run")
    def test_all_checks_pass(self, mock_run):
        """Test when all CI checks pass."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='[{"state": "pass", "name": "test"}, {"state": "pass", "name": "lint"}]',
        )
        resolved, error = check_pr_ci("gptme/gptme#1217")
        assert resolved is True
        assert error is None

    @patch("subprocess.run")
    def test_some_checks_fail(self, mock_run):
        """Test when some CI checks fail."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='[{"state": "pass", "name": "test"}, {"state": "failure", "name": "lint"}]',
        )
        resolved, error = check_pr_ci("gptme/gptme#1217")
        assert resolved is False
        assert "lint" in error

    @patch("subprocess.run")
    def test_no_checks(self, mock_run):
        """Test when no CI checks found."""
        mock_run.return_value = MagicMock(returncode=0, stdout="[]")
        resolved, error = check_pr_ci("gptme/gptme#1217")
        assert resolved is False
        assert "No CI checks" in error


class TestCheckPrMerged:
    """Test PR merged checking."""

    @patch("subprocess.run")
    def test_pr_merged(self, mock_run):
        """Test when PR is merged."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"state": "MERGED", "merged": true}',
        )
        resolved, error = check_pr_merged("gptme/gptme#1217")
        assert resolved is True
        assert error is None

    @patch("subprocess.run")
    def test_pr_not_merged(self, mock_run):
        """Test when PR is not merged."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"state": "OPEN", "merged": false}',
        )
        resolved, error = check_pr_merged("gptme/gptme#1217")
        assert resolved is False
        assert "not yet merged" in error


class TestCheckCondition:
    """Test the unified check_condition function."""

    def test_task_type_skipped(self):
        """Test that TASK type doesn't change state."""
        cond = WaitCondition(type=WaitType.TASK, ref="other-task")
        result = check_condition(cond)
        assert result.resolved is False  # Not checked, left for unblock.py

    @patch("gptodo.waiting.check_pr_ci")
    def test_pr_ci_resolved(self, mock_check):
        """Test PR CI condition resolution."""
        mock_check.return_value = (True, None)
        cond = WaitCondition(type=WaitType.PR_CI, ref="gptme/gptme#1217")
        result = check_condition(cond)
        assert result.resolved is True
        assert result.resolution_time is not None
