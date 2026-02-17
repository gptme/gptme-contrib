"""Tests for github_data module."""

from datetime import date
from unittest.mock import patch

from gptme_activity_summary.github_data import (
    GitHubActivity,
    RepoActivity,
    _run_command,
    fetch_user_activity,
    format_activity_for_prompt,
    get_cross_repo_prs,
    get_merged_prs,
    get_user_commits,
    get_user_issues,
    get_user_prs,
)


def test_run_command_success():
    """Test _run_command with a successful command."""
    result = _run_command(["echo", "hello"])
    assert result == "hello"


def test_run_command_failure():
    """Test _run_command returns None on failure."""
    result = _run_command(["false"])
    assert result is None


def test_run_command_not_found():
    """Test _run_command returns None when command not found."""
    result = _run_command(["nonexistent_command_12345"])
    assert result is None


def test_format_activity_empty():
    """Test formatting with no activity."""
    activity = GitHubActivity(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 7),
    )
    result = format_activity_for_prompt(activity)
    assert result == ""


def test_format_activity_with_data():
    """Test formatting with actual data."""
    activity = GitHubActivity(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 7),
        repos=[
            RepoActivity(
                repo="owner/repo",
                commits=5,
                merged_prs=[
                    {
                        "number": "42",
                        "title": "Add feature X",
                        "url": "https://github.com/owner/repo/pull/42",
                    },
                ],
                closed_issues=[
                    {
                        "number": "10",
                        "title": "Bug in Y",
                        "url": "https://github.com/owner/repo/issues/10",
                    },
                ],
            ),
        ],
    )
    result = format_activity_for_prompt(activity)
    assert "GitHub Activity (Real Data)" in result
    assert "Total commits**: 5" in result
    assert "PRs merged**: 1" in result
    assert "Issues closed**: 1" in result
    assert "#42: Add feature X" in result
    assert "#10: Bug in Y" in result


def test_format_activity_multiple_repos():
    """Test formatting with multiple repos."""
    activity = GitHubActivity(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 7),
        repos=[
            RepoActivity(repo="owner/repo1", commits=3),
            RepoActivity(repo="owner/repo2", commits=2),
        ],
    )
    result = format_activity_for_prompt(activity)
    assert "Total commits**: 5" in result
    assert "owner/repo1" in result
    assert "owner/repo2" in result


def test_get_merged_prs_parses_json():
    """Test get_merged_prs correctly parses gh output."""
    mock_output = '[{"number": 1, "title": "Fix bug", "url": "https://github.com/o/r/pull/1", "mergedAt": "2025-01-01T00:00:00Z"}]'
    with patch("gptme_activity_summary.github_data._run_command", return_value=mock_output):
        prs = get_merged_prs(date(2025, 1, 1), date(2025, 1, 7), "o/r")
    assert len(prs) == 1
    assert prs[0]["number"] == "1"
    assert prs[0]["title"] == "Fix bug"


def test_get_merged_prs_handles_none():
    """Test get_merged_prs returns empty list when command fails."""
    with patch("gptme_activity_summary.github_data._run_command", return_value=None):
        prs = get_merged_prs(date(2025, 1, 1), date(2025, 1, 7), "o/r")
    assert prs == []


def test_github_activity_properties():
    """Test GitHubActivity computed properties."""
    activity = GitHubActivity(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 7),
        repos=[
            RepoActivity(
                repo="repo1",
                commits=3,
                merged_prs=[{"number": "1", "title": "PR1", "url": ""}],
                closed_issues=[{"number": "1", "title": "I1", "url": ""}],
            ),
            RepoActivity(
                repo="repo2",
                commits=2,
                merged_prs=[
                    {"number": "2", "title": "PR2", "url": ""},
                    {"number": "3", "title": "PR3", "url": ""},
                ],
            ),
        ],
    )
    assert activity.total_commits == 5
    assert activity.total_prs_merged == 3
    assert activity.total_issues_closed == 1


def test_get_cross_repo_prs_excludes_defaults():
    """Test get_cross_repo_prs respects exclude_repos parameter."""
    mock_output = '[{"repository": {"nameWithOwner": "other/repo"}, "number": 1, "title": "Fix", "state": "MERGED", "url": ""}]'
    with patch("gptme_activity_summary.github_data._run_command", return_value=mock_output):
        prs = get_cross_repo_prs(
            date(2025, 1, 1),
            date(2025, 1, 7),
            author="testuser",
            exclude_repos=["excluded/repo"],
        )
    assert len(prs) == 1
    assert prs[0].repo == "other/repo"


def test_get_cross_repo_prs_filters_excluded():
    """Test get_cross_repo_prs filters out excluded repos."""
    mock_output = '[{"repository": {"nameWithOwner": "excluded/repo"}, "number": 1, "title": "Fix", "state": "MERGED", "url": ""}]'
    with patch("gptme_activity_summary.github_data._run_command", return_value=mock_output):
        prs = get_cross_repo_prs(
            date(2025, 1, 1),
            date(2025, 1, 7),
            author="testuser",
            exclude_repos=["excluded/repo"],
        )
    assert len(prs) == 0


def test_get_user_prs():
    """Test get_user_prs parses search results."""
    mock_output = '[{"repository": {"nameWithOwner": "user/repo"}, "number": 42, "title": "Add feature", "state": "MERGED", "url": "https://github.com/user/repo/pull/42"}]'
    with patch("gptme_activity_summary.github_data._run_command", return_value=mock_output):
        prs = get_user_prs(date(2025, 1, 1), date(2025, 1, 7), "testuser")
    assert len(prs) == 1
    assert prs[0].repo == "user/repo"
    assert prs[0].number == 42
    assert prs[0].title == "Add feature"


def test_get_user_prs_handles_none():
    """Test get_user_prs returns empty list when command fails."""
    with patch("gptme_activity_summary.github_data._run_command", return_value=None):
        prs = get_user_prs(date(2025, 1, 1), date(2025, 1, 7), "testuser")
    assert prs == []


def test_get_user_issues():
    """Test get_user_issues parses search results."""
    mock_output = '[{"repository": {"nameWithOwner": "user/repo"}, "number": 10, "title": "Bug report", "state": "OPEN", "url": ""}]'
    with patch("gptme_activity_summary.github_data._run_command", return_value=mock_output):
        issues = get_user_issues(date(2025, 1, 1), date(2025, 1, 7), "testuser")
    assert len(issues) == 1
    assert issues[0]["repo"] == "user/repo"
    assert issues[0]["number"] == "10"


def test_get_user_issues_handles_none():
    """Test get_user_issues returns empty list when command fails."""
    with patch("gptme_activity_summary.github_data._run_command", return_value=None):
        issues = get_user_issues(date(2025, 1, 1), date(2025, 1, 7), "testuser")
    assert issues == []


def test_get_user_commits():
    """Test get_user_commits counts search results."""
    mock_output = '[{"sha": "abc123"}, {"sha": "def456"}, {"sha": "ghi789"}]'
    with patch("gptme_activity_summary.github_data._run_command", return_value=mock_output):
        count = get_user_commits(date(2025, 1, 1), date(2025, 1, 7), "testuser")
    assert count == 3


def test_get_user_commits_handles_none():
    """Test get_user_commits returns 0 when command fails."""
    with patch("gptme_activity_summary.github_data._run_command", return_value=None):
        count = get_user_commits(date(2025, 1, 1), date(2025, 1, 7), "testuser")
    assert count == 0


def test_fetch_user_activity():
    """Test fetch_user_activity aggregates data from multiple sources."""
    pr_output = '[{"repository": {"nameWithOwner": "user/repo1"}, "number": 1, "title": "PR1", "state": "MERGED", "url": ""}]'
    issue_output = '[{"repository": {"nameWithOwner": "user/repo1"}, "number": 10, "title": "Issue1", "state": "CLOSED", "url": ""}]'
    commit_output = '[{"sha": "abc"}, {"sha": "def"}]'

    def mock_run(cmd, timeout=30):
        cmd_str = " ".join(cmd)
        if "search prs" in cmd_str:
            return pr_output
        elif "search issues" in cmd_str:
            return issue_output
        elif "search commits" in cmd_str:
            return commit_output
        elif "auth status" in cmd_str:
            return "ok"
        return None

    with patch("gptme_activity_summary.github_data._run_command", side_effect=mock_run):
        activity = fetch_user_activity(date(2025, 1, 1), date(2025, 1, 7), "testuser")

    assert len(activity.repos) >= 1
    assert activity.repos[0].repo == "user/repo1"
    assert activity.repos[0].commits == 2
