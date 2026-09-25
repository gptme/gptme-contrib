"""Tests for github_data module."""

import json
import os
import shutil
import subprocess
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

from gptme_activity_summary.github_data import (
    LIST_LIMIT,
    PROJECT_REPOS,
    GitHubActivity,
    RepoActivity,
    UserEvent,
    _canonical_repo,
    _render_event_line,
    _run_command,
    _search_total_count,
    default_repos,
    detect_workspace_repo,
    fetch_activity,
    fetch_user_activity,
    format_activity_for_prompt,
    get_commit_count,
    get_cross_repo_prs,
    get_merged_prs,
    get_user_commits,
    get_user_events,
    get_user_issues,
    get_user_prs,
    repo_from_remote_url,
)


def _init_repo_with_remote(path, url: str) -> None:
    """Create a git repo at ``path`` whose ``origin`` is ``url``."""
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", url], check=True)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:NewAgent/agent-brain.git", "NewAgent/agent-brain"),
        ("https://github.com/NewAgent/agent-brain.git", "NewAgent/agent-brain"),
        ("https://github.com/NewAgent/agent-brain", "NewAgent/agent-brain"),
        ("ssh://git@github.com/NewAgent/agent-brain.git", "NewAgent/agent-brain"),
        ("https://github.com/NewAgent/agent-brain/", "NewAgent/agent-brain"),
        ("https://github.com/NewAgent/agent-brain.git/", "NewAgent/agent-brain"),
        ("git@github.com:NewAgent/agent-brain.git/", "NewAgent/agent-brain"),
        ("", None),
        ("/home/bob/bob", None),
        ("https://github.com/only-owner", None),
    ],
)
def test_repo_from_remote_url(url: str, expected: str | None):
    """Remote URL spellings all resolve to a single owner/name."""
    assert repo_from_remote_url(url) == expected


def test_detect_workspace_repo_reads_origin(tmp_path):
    """The workspace repo comes from its origin remote."""
    _init_repo_with_remote(tmp_path, "git@github.com:NewAgent/agent-brain.git")

    assert detect_workspace_repo(tmp_path) == "NewAgent/agent-brain"


def test_detect_workspace_repo_none_without_remote_or_workspace(tmp_path):
    """No remote (or no workspace) means no detectable repo."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)

    assert detect_workspace_repo(tmp_path) is None
    assert detect_workspace_repo(None) is None


def test_default_repos_derives_workspace_repo_not_bob(tmp_path):
    """A non-Bob workspace must summarize its own repo, never ErikBjare/gptme-bob."""
    _init_repo_with_remote(tmp_path, "git@github.com:NewAgent/agent-brain.git")

    repos = default_repos(tmp_path)

    assert repos[0] == "NewAgent/agent-brain"
    assert repos[1:] == PROJECT_REPOS
    assert "ErikBjare/gptme-bob" not in repos


def test_default_repos_without_workspace_repo_has_no_bob_repo(tmp_path):
    """With no remote to derive from, no Bob-specific repo may appear."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)

    assert default_repos(tmp_path) == PROJECT_REPOS
    assert default_repos(None) == PROJECT_REPOS
    assert "ErikBjare/gptme-bob" not in default_repos(None)


def test_fetch_activity_defaults_to_workspace_repo(tmp_path):
    """The default fetch target follows the workspace, not a hard-coded repo."""
    _init_repo_with_remote(tmp_path, "git@github.com:NewAgent/agent-brain.git")

    with patch("gptme_activity_summary.github_data._gh_available", return_value=False):
        activity = fetch_activity(date(2026, 8, 1), date(2026, 8, 1), workspace=str(tmp_path))

    fetched = [repo.repo for repo in activity.repos]
    assert fetched == ["NewAgent/agent-brain", *PROJECT_REPOS]
    assert "ErikBjare/gptme-bob" not in fetched


def test_default_repos_dedupes_project_repo_workspace(tmp_path):
    """A workspace that *is* a project repo is not summarized twice."""
    _init_repo_with_remote(tmp_path, "git@github.com:gptme/gptme-contrib.git")

    repos = default_repos(tmp_path)

    assert repos == ["gptme/gptme-contrib", "gptme/gptme"]
    assert len(repos) == len(set(repos))


def test_fetch_activity_attributes_commits_to_workspace_repo(tmp_path):
    """Local commits belong to the workspace repo, not to a project repo."""
    _init_repo_with_remote(tmp_path, "git@github.com:NewAgent/agent-brain.git")

    with (
        patch("gptme_activity_summary.github_data._gh_available", return_value=False),
        patch("gptme_activity_summary.github_data.get_commit_count", return_value=7),
    ):
        activity = fetch_activity(date(2026, 8, 1), date(2026, 8, 1), workspace=str(tmp_path))

    assert activity.repos[0].repo == "NewAgent/agent-brain"
    assert activity.repos[0].commits == 7
    assert activity.total_commits == 7


def test_fetch_activity_does_not_credit_project_repo_without_workspace_repo(tmp_path):
    """With no derivable workspace repo, commits stay 'local', not gptme/gptme."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)

    with (
        patch("gptme_activity_summary.github_data._gh_available", return_value=False),
        patch("gptme_activity_summary.github_data.get_commit_count", return_value=5),
    ):
        activity = fetch_activity(date(2026, 8, 1), date(2026, 8, 1), workspace=str(tmp_path))

    by_name = {repo.repo: repo.commits for repo in activity.repos}
    assert by_name["local"] == 5
    assert all(by_name[repo] == 0 for repo in PROJECT_REPOS)
    assert activity.total_commits == 5


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


# --- Exact counts (not capped by list limits) ---


def _items(n: int) -> str:
    return json.dumps([{"number": i, "title": f"T{i}", "url": ""} for i in range(n)])


def _search_query(cmd: list[str]) -> str | None:
    """Extract the q= value from a `gh api search/...` command, else None."""
    if "api" not in cmd or not any(c.startswith("search/") for c in cmd):
        return None
    return next(c[2:] for c in cmd if c.startswith("q="))


def test_search_total_count_parses_and_requests_one_item():
    with patch("gptme_activity_summary.github_data._run_command", return_value="472") as mock_run:
        assert _search_total_count("author:x is:pr is:merged") == 472
    cmd = mock_run.call_args[0][0]
    assert "search/issues" in cmd
    assert "per_page=1" in cmd
    assert "q=author:x is:pr is:merged" in cmd


def test_search_total_count_handles_failure():
    with patch("gptme_activity_summary.github_data._run_command", return_value=None):
        assert _search_total_count("q") is None
    with patch("gptme_activity_summary.github_data._run_command", return_value="oops"):
        assert _search_total_count("q") is None


def test_canonical_repo_follows_rename():
    with patch(
        "gptme_activity_summary.github_data._run_command", return_value="new/name"
    ) as mock_run:
        assert _canonical_repo("old/name") == "new/name"
    cmd = mock_run.call_args[0][0]
    assert cmd[:2] == ["gh", "api"]
    assert cmd[2] == "repos/old/name"
    assert "--jq" in cmd
    assert ".full_name" in cmd


def test_canonical_repo_keeps_input_on_failure():
    with patch("gptme_activity_summary.github_data._run_command", return_value=None):
        assert _canonical_repo("old/name") == "old/name"
    with patch("gptme_activity_summary.github_data._run_command", return_value="[]"):
        assert _canonical_repo("old/name") == "old/name"


def test_fetch_activity_searches_canonical_repo_name():
    """Stale nwo must be resolved before search, or counts come back as zero."""
    seen_queries: list[str] = []

    def mock_run(cmd, timeout=30):
        if cmd[:3] == ["gh", "auth", "status"]:
            return "ok"
        if cmd[:2] == ["gh", "api"] and len(cmd) > 2 and str(cmd[2]).startswith("repos/"):
            nwo = str(cmd[2]).removeprefix("repos/")
            return {"old/name": "new/name"}.get(nwo, nwo)
        query = _search_query(cmd)
        if query is not None:
            seen_queries.append(query)
            return "4" if "is:pr" in query else "1"
        if cmd[1] == "pr" and "list" in cmd:
            assert cmd[cmd.index("--repo") + 1] == "new/name"
            return _items(4)
        if cmd[1] == "issue":
            return _items(1)
        return "[]"

    with patch("gptme_activity_summary.github_data._run_command", side_effect=mock_run):
        activity = fetch_activity(date(2026, 8, 1), date(2026, 8, 31), repos=["old/name"])

    assert activity.repos[0].repo == "new/name"
    assert activity.total_prs_merged == 4
    assert any(q.startswith("repo:new/name ") and "is:pr" in q for q in seen_queries)
    assert not any("repo:old/name" in q for q in seen_queries)


def test_fetch_activity_excludes_canonical_repo_from_cross_repo_prs():
    """A renamed default repo must not reappear as a cross-repo PR."""

    def mock_run(cmd, timeout=30):
        if cmd[:3] == ["gh", "auth", "status"]:
            return "ok"
        if cmd[:2] == ["gh", "api"] and len(cmd) > 2 and str(cmd[2]).startswith("repos/"):
            nwo = str(cmd[2]).removeprefix("repos/")
            return {"old/name": "new/name"}.get(nwo, nwo)
        query = _search_query(cmd)
        if query is not None:
            return "1"
        if cmd[1] == "search" and "prs" in cmd:
            return json.dumps(
                [
                    {
                        "repository": {"nameWithOwner": "new/name"},
                        "number": 1,
                        "title": "in canonical repo",
                        "state": "MERGED",
                        "url": "",
                    },
                    {
                        "repository": {"nameWithOwner": "other/repo"},
                        "number": 2,
                        "title": "actually cross-repo",
                        "state": "MERGED",
                        "url": "",
                    },
                ]
            )
        return "[]"

    with patch("gptme_activity_summary.github_data._run_command", side_effect=mock_run):
        activity = fetch_activity(date(2026, 8, 1), date(2026, 8, 31), repos=["old/name"])

    assert [pr.repo for pr in activity.cross_repo_prs] == ["other/repo"]


def test_repo_activity_count_prefers_exact_total():
    repo = RepoActivity(repo="o/r", merged_prs=json.loads(_items(100)), merged_prs_total=208)
    assert repo.merged_prs_count == 208
    assert RepoActivity(repo="o/r", merged_prs=json.loads(_items(3))).merged_prs_count == 3


def test_fetch_activity_reports_true_count_beyond_list_limit():
    """A month with >200 merged PRs must not be reported as 100-per-repo."""
    totals = {"a/one": 208, "b/two": 211, "c/three": 33}
    issue_totals = {"a/one": 37, "b/two": 5, "c/three": 144}

    def mock_run(cmd, timeout=30):
        if cmd[:3] == ["gh", "auth", "status"]:
            return "ok"
        query = _search_query(cmd)
        if query is not None:
            repo = query.split()[0].removeprefix("repo:")
            return str(totals[repo] if "is:pr" in query else issue_totals[repo])
        if "--repo" not in cmd:  # cross-repo `gh search prs`
            return "[]"
        repo = cmd[cmd.index("--repo") + 1]
        if "reviews" in " ".join(cmd):  # get_reviews_received
            return "[]"
        if cmd[1] == "pr" and "list" in cmd:
            return _items(min(totals[repo], LIST_LIMIT))
        if cmd[1] == "issue":
            return _items(min(issue_totals[repo], LIST_LIMIT))
        return "[]"

    with patch("gptme_activity_summary.github_data._run_command", side_effect=mock_run):
        activity = fetch_activity(date(2026, 8, 1), date(2026, 8, 31), repos=list(totals))

    # Detail lists stay truncated (100 + 100 + 33); the old count was len() of these.
    assert [len(r.merged_prs) for r in activity.repos] == [100, 100, 33]
    assert activity.total_prs_merged == 452
    assert activity.total_issues_closed == 186

    text = format_activity_for_prompt(activity)
    assert "**PRs merged**: 452" in text
    assert "- PRs merged: 208 (showing 100)" in text
    assert "- Issues closed: 144 (showing 100)" in text


def test_fetch_activity_falls_back_to_list_length_when_count_fails():
    def mock_run(cmd, timeout=30):
        if cmd[:3] == ["gh", "auth", "status"]:
            return "ok"
        if _search_query(cmd) is not None:
            return None
        return _items(7) if cmd[1] == "pr" else _items(2)

    with patch("gptme_activity_summary.github_data._run_command", side_effect=mock_run):
        activity = fetch_activity(date(2026, 8, 1), date(2026, 8, 31), repos=["o/r"])
    assert activity.total_prs_merged == 7
    assert activity.total_issues_closed == 2


def test_fetch_user_activity_uses_exact_totals():
    def mock_run(cmd, timeout=30):
        cmd_str = " ".join(cmd)
        if "auth status" in cmd_str:
            return "ok"
        query = _search_query(cmd)
        if query is not None:
            if "search/commits" in cmd:
                return "16457"
            return "556" if "is:pr" in query else "12"
        if "search prs" in cmd_str:
            return json.dumps(
                [
                    {"repository": {"nameWithOwner": "u/r"}, "number": i, "title": "", "url": ""}
                    for i in range(LIST_LIMIT)
                ]
            )
        if "search issues" in cmd_str:
            return "[]"
        return None

    with patch("gptme_activity_summary.github_data._run_command", side_effect=mock_run):
        activity = fetch_user_activity(date(2026, 8, 1), date(2026, 8, 31), "someone")
    assert activity.total_prs_merged == 556
    assert activity.total_issues_closed == 12
    assert activity.total_commits == 16457


def test_fetch_user_activity_drops_open_items_from_merged_lists():
    """Per-repo 'Merged PRs' / 'Closed Issues' lists must not include open items."""

    def mock_run(cmd, timeout=30):
        cmd_str = " ".join(cmd)
        if "auth status" in cmd_str:
            return "ok"
        if cmd[:3] == ["gh", "search", "prs"]:
            return json.dumps(
                [
                    {
                        "repository": {"nameWithOwner": "u/r"},
                        "number": 1,
                        "title": "merged",
                        "state": "MERGED",
                        "url": "",
                    },
                    {
                        "repository": {"nameWithOwner": "u/r"},
                        "number": 2,
                        "title": "still open",
                        "state": "OPEN",
                        "url": "",
                    },
                ]
            )
        if cmd[:3] == ["gh", "search", "issues"]:
            return json.dumps(
                [
                    {
                        "repository": {"nameWithOwner": "u/r"},
                        "number": 10,
                        "title": "closed",
                        "state": "CLOSED",
                        "url": "",
                    },
                    {
                        "repository": {"nameWithOwner": "u/r"},
                        "number": 11,
                        "title": "still open",
                        "state": "OPEN",
                        "url": "",
                    },
                ]
            )
        return None

    with patch("gptme_activity_summary.github_data._run_command", side_effect=mock_run):
        activity = fetch_user_activity(date(2026, 8, 1), date(2026, 8, 31), "someone")

    assert [pr["number"] for pr in activity.repos[0].merged_prs] == ["1"]
    assert [issue["number"] for issue in activity.repos[0].closed_issues] == ["10"]


def test_get_user_commits_uses_exact_total():
    with patch("gptme_activity_summary.github_data._run_command", return_value="16457"):
        assert get_user_commits(date(2026, 8, 1), date(2026, 8, 31), "someone") == 16457


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
@pytest.mark.parametrize("tz", ["UTC", "America/Los_Angeles", "Europe/Stockholm"])
def test_get_commit_count_excludes_adjacent_days(tmp_path, monkeypatch, tz):
    """Commits just outside [start, end] must not leak in via time-of-day bounds.

    Bounds are UTC; the process timezone must not change which commits count.
    """
    monkeypatch.setenv("TZ", tz)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for stamp in (
        "2025-01-01T23:59:59+0000",  # last second of the day before
        "2025-01-02T00:00:00+0000",  # exact start (inclusive)
        "2025-01-02T00:00:01+0000",
        "2025-01-03T23:59:00+0000",
        "2025-01-03T23:59:59+0000",  # exact end (inclusive)
        "2025-01-04T00:00:00+0000",  # first second of the day after
    ):
        env = {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.com",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                stamp,
            ],
            check=True,
            env={**os.environ, **env},
        )
    assert get_commit_count(date(2025, 1, 2), date(2025, 1, 3), str(tmp_path)) == 4


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


def test_get_user_prs_searches_by_merged_at():
    """Human-mode PR lists must use merge date, not creation date."""
    captured: list[list[str]] = []

    def mock_run(cmd, timeout=30):
        captured.append(cmd)
        return "[]"

    with patch("gptme_activity_summary.github_data._run_command", side_effect=mock_run):
        get_user_prs(date(2026, 8, 1), date(2026, 8, 31), "someone")

    assert captured
    cmd = captured[0]
    assert "--merged-at" in cmd
    assert cmd[cmd.index("--merged-at") + 1] == "2026-08-01..2026-08-31"
    assert "--merged" in cmd
    assert "--created" not in cmd


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


def test_get_user_issues_searches_by_closed_date():
    """Human-mode issue lists must use close date, not creation date."""
    captured: list[list[str]] = []

    def mock_run(cmd, timeout=30):
        captured.append(cmd)
        return "[]"

    with patch("gptme_activity_summary.github_data._run_command", side_effect=mock_run):
        get_user_issues(date(2026, 8, 1), date(2026, 8, 31), "someone")

    assert captured
    cmd = captured[0]
    assert "--closed" in cmd
    assert cmd[cmd.index("--closed") + 1] == "2026-08-01..2026-08-31"
    assert "--state" in cmd
    assert cmd[cmd.index("--state") + 1] == "closed"
    assert "--created" not in cmd


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


def test_render_event_release():
    line = _render_event_line(
        _make_event(
            "ReleaseEvent",
            {
                "action": "published",
                "release": {"tag_name": "v1.2.3", "name": "Spring release"},
            },
        )
    )
    assert line == "Release published owner/repo: v1.2.3 (Spring release)"


def test_render_event_commit_comment():
    line = _render_event_line(
        _make_event(
            "CommitCommentEvent",
            {
                "comment": {
                    "commit_id": "abcdef1234567890",
                    "body": "this changed behavior, see issue #5",
                },
            },
        )
    )
    assert line == "Commit comment owner/repo@abcdef1: this changed behavior, see issue #5"


def test_render_event_unknown_type_falls_back_to_generic():
    # Defense: unknown event types should at least surface the type + repo
    # rather than crash or silently drop.
    line = _render_event_line(_make_event("SomeNewEventType", {}))
    assert line == "SomeNewEventType owner/repo"


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


def test_get_user_events_early_exit_when_page_predates_start():
    """API returns newest-first; if oldest event on a page is before start,
    later pages will only be older — stop paging early."""
    # Page 1: all events are well before the start of our window (2025-02-01).
    page1 = [
        _make_event(
            "PullRequestReviewEvent",
            {"pull_request": {"number": 1, "title": "Old"}, "review": {"state": "approved"}},
            created_at="2024-12-15T10:00:00Z",
        ),
        _make_event(
            "PullRequestReviewEvent",
            {"pull_request": {"number": 2, "title": "Older"}, "review": {"state": "approved"}},
            created_at="2024-12-10T10:00:00Z",
        ),
    ]
    # If pagination doesn't early-exit, side_effect would need more entries.
    with patch(
        "gptme_activity_summary.github_data._run_command",
        side_effect=[json.dumps(page1)],
    ) as mock_cmd:
        result = get_user_events(date(2025, 2, 1), date(2025, 2, 7), "testuser")
    # Should have stopped after page 1 (oldest event predates start).
    assert mock_cmd.call_count == 1
    # No events in window
    assert result == []


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
