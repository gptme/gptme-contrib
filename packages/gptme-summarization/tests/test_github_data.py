"""Tests for github_data module."""

from datetime import date
from unittest.mock import patch

from gptme_summarization.github_data import (
    GitHubActivity,
    RepoActivity,
    _run_command,
    format_activity_for_prompt,
    get_merged_prs,
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
    with patch("gptme_summarization.github_data._run_command", return_value=mock_output):
        prs = get_merged_prs(date(2025, 1, 1), date(2025, 1, 7), "o/r")
    assert len(prs) == 1
    assert prs[0]["number"] == "1"
    assert prs[0]["title"] == "Fix bug"


def test_get_merged_prs_handles_none():
    """Test get_merged_prs returns empty list when command fails."""
    with patch("gptme_summarization.github_data._run_command", return_value=None):
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
