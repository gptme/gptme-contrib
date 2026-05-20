"""Tests for github_data module."""

import json
from datetime import date, datetime, timezone
from unittest.mock import patch

from gptme_activity_summary.github_data import (
    GitHubActivity,
    RepoActivity,
    UserEvent,
    _render_event_line,
    _run_command,
    fetch_user_activity,
    format_activity_for_prompt,
    get_cross_repo_prs,
    get_merged_prs,
    get_user_commits,
    get_user_events,
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


# --- Event rendering ---


def _make_event(etype: str, payload: dict, created_at: str = "2025-01-03T12:00:00Z") -> dict:
    return {
        "type": etype,
        "repo": {"name": "owner/repo"},
        "created_at": created_at,
        "payload": payload,
    }


def test_render_event_pull_request_review():
    line = _render_event_line(
        _make_event(
            "PullRequestReviewEvent",
            {
                "pull_request": {"number": 42, "title": "Add feature"},
                "review": {"state": "approved"},
            },
        )
    )
    assert line == "PR review (approved) owner/repo#42: Add feature"


def test_render_event_pull_request_review_comment_truncates_body():
    body = "looks good but consider X" + " padding" * 30
    line = _render_event_line(
        _make_event(
            "PullRequestReviewCommentEvent",
            {
                "pull_request": {"number": 7, "title": "Refactor X"},
                "comment": {"body": body},
            },
        )
    )
    assert line is not None
    assert "PR review-comment owner/repo#7" in line
    assert "looks good but consider X" in line


def test_render_event_issue_comment_distinguishes_pr_vs_issue():
    pr_comment = _render_event_line(
        _make_event(
            "IssueCommentEvent",
            {
                # GitHub sets issue.pull_request to a non-empty dict when the
                # issue is actually a PR.
                "issue": {
                    "number": 5,
                    "title": "Bug",
                    "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/5"},
                },
                "comment": {"body": "thanks"},
            },
        )
    )
    issue_comment = _render_event_line(
        _make_event(
            "IssueCommentEvent",
            {
                "issue": {"number": 6, "title": "Question"},
                "comment": {"body": "see docs"},
            },
        )
    )
    assert pr_comment is not None and pr_comment.startswith("PR comment ")
    assert issue_comment is not None and issue_comment.startswith("Issue comment ")


def test_render_event_push_skips_empty_commits():
    line = _render_event_line(
        _make_event(
            "PushEvent",
            {"ref": "refs/heads/main", "commits": []},
        )
    )
    assert line is None


def test_render_event_push_renders_with_messages():
    line = _render_event_line(
        _make_event(
            "PushEvent",
            {
                "ref": "refs/heads/main",
                "commits": [
                    {"message": "fix: bug A"},
                    {"message": "feat: thing B"},
                ],
            },
        )
    )
    assert line is not None
    assert "push owner/repo (main) — 2 commits" in line
    assert "fix: bug A" in line


def test_render_event_drops_noise_types():
    # Noisy / non-productivity types, plus types that duplicate search-based data.
    for noisy in (
        "WatchEvent",
        "CreateEvent",
        "DeleteEvent",
        "ForkEvent",
        "PullRequestEvent",
        "IssuesEvent",
    ):
        assert _render_event_line(_make_event(noisy, {})) is None


# --- Event fetching ---


def test_get_user_events_filters_by_date_range_and_sorts_chronologically():
    events = [
        _make_event(
            "PullRequestReviewEvent",
            {"pull_request": {"number": 1, "title": "A"}, "review": {"state": "approved"}},
            created_at="2025-01-05T10:00:00Z",
        ),
        _make_event(
            "PullRequestReviewEvent",
            {"pull_request": {"number": 2, "title": "B"}, "review": {"state": "approved"}},
            created_at="2025-01-03T09:00:00Z",
        ),
        # Outside window
        _make_event(
            "PullRequestReviewEvent",
            {"pull_request": {"number": 3, "title": "C"}, "review": {"state": "approved"}},
            created_at="2025-01-10T09:00:00Z",
        ),
    ]
    with patch(
        "gptme_activity_summary.github_data._run_command",
        side_effect=[json.dumps(events), "[]"],
    ):
        result = get_user_events(date(2025, 1, 1), date(2025, 1, 7), "testuser")
    assert [e.line for e in result] == [
        "PR review (approved) owner/repo#2: B",
        "PR review (approved) owner/repo#1: A",
    ]


def test_get_user_events_paginates_until_empty():
    page1 = [
        _make_event(
            "IssueCommentEvent",
            {"issue": {"number": 9, "title": "Q"}, "comment": {"body": "x"}},
            created_at="2025-01-05T12:00:00Z",
        )
    ]
    side_effects = [json.dumps(page1), "[]"]
    with patch(
        "gptme_activity_summary.github_data._run_command",
        side_effect=side_effects,
    ) as mock_cmd:
        result = get_user_events(date(2025, 1, 1), date(2025, 1, 7), "testuser")
    # One real page returned data, second page returned empty → stops
    assert mock_cmd.call_count == 2
    assert len(result) == 1


def test_get_user_events_handles_none_response():
    with patch(
        "gptme_activity_summary.github_data._run_command",
        return_value=None,
    ):
        result = get_user_events(date(2025, 1, 1), date(2025, 1, 7), "testuser")
    assert result == []


def test_fetch_user_activity_populates_events():
    """fetch_user_activity should call get_user_events and attach results."""
    review_event = _make_event(
        "PullRequestReviewEvent",
        {"pull_request": {"number": 1, "title": "X"}, "review": {"state": "approved"}},
        created_at="2025-01-03T12:00:00Z",
    )

    def mock_run(cmd, timeout=30):
        cmd_str = " ".join(cmd)
        if "auth status" in cmd_str:
            return "ok"
        if "search prs" in cmd_str:
            return "[]"
        if "search issues" in cmd_str:
            return "[]"
        if "search commits" in cmd_str:
            return "[]"
        if "users/" in cmd_str and "events/public" in cmd_str:
            # First page has data; subsequent pages empty.
            # (Match "&page=1" not "page=1" to avoid colliding with per_page=100.)
            if "&page=1" in cmd_str:
                return json.dumps([review_event])
            return "[]"
        return None

    with patch("gptme_activity_summary.github_data._run_command", side_effect=mock_run):
        activity = fetch_user_activity(date(2025, 1, 1), date(2025, 1, 7), "testuser")

    assert len(activity.events) == 1
    assert activity.events[0].type == "PullRequestReviewEvent"
    assert activity.events[0].line.startswith("PR review (approved)")


# --- Formatting ---


def test_format_activity_includes_events_block():
    activity = GitHubActivity(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 7),
        repos=[RepoActivity(repo="owner/repo", commits=1)],
        events=[
            UserEvent(
                type="PullRequestReviewEvent",
                repo="owner/repo",
                timestamp=datetime(2025, 1, 3, 12, 0, tzinfo=timezone.utc),
                line="PR review (approved) owner/repo#1: X",
            ),
            UserEvent(
                type="IssueCommentEvent",
                repo="owner/repo",
                timestamp=datetime(2025, 1, 4, 12, 0, tzinfo=timezone.utc),
                line="PR comment owner/repo#2: Y — nice",
            ),
        ],
    )
    out = format_activity_for_prompt(activity)
    assert "### GitHub Events (extended signal)" in out
    assert "PullRequestReviewEvent:1" in out
    assert "IssueCommentEvent:1" in out
    assert "- PR review (approved) owner/repo#1: X" in out


def test_format_activity_events_only_still_renders():
    """If only events exist (no PRs/issues/commits), still emit the block."""
    activity = GitHubActivity(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 7),
        events=[
            UserEvent(
                type="PullRequestReviewEvent",
                repo="owner/repo",
                timestamp=datetime(2025, 1, 3, 12, 0, tzinfo=timezone.utc),
                line="PR review (approved) owner/repo#1: X",
            ),
        ],
    )
    out = format_activity_for_prompt(activity)
    assert "GitHub Activity (Real Data)" in out
    assert "### GitHub Events (extended signal)" in out
