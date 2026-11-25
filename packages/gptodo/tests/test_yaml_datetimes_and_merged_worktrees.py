"""Regression tests for gptme/gptme-contrib#2304 items 1, 7 and 8.

YAML turns an unquoted ISO timestamp into a ``datetime`` (and a bare date into a
``date``), so frontmatter values reach these helpers as objects, not strings.
"""

from __future__ import annotations

import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import yaml
from gptodo.utils import has_new_activity
from gptodo.waiting import check_time
from gptodo.worktree import cleanup_merged_worktrees, get_worktrees_dir


def test_has_new_activity_accepts_yaml_datetime_waiting_since() -> None:
    waiting_since = yaml.safe_load("w: 2026-11-20T10:00:00Z")["w"]
    assert isinstance(waiting_since, datetime)
    assert has_new_activity("2026-11-21T10:00:00Z", waiting_since)
    assert not has_new_activity("2026-11-19T10:00:00Z", waiting_since)


def test_has_new_activity_naive_datetime_waiting_since() -> None:
    assert has_new_activity("2026-11-21T10:00:00Z", datetime(2026, 11, 20, 10))


def test_check_time_accepts_yaml_datetime_ref() -> None:
    ref = yaml.safe_load("ref: 2020-01-01T00:00:00Z")["ref"]
    assert isinstance(ref, datetime)
    assert check_time(ref) == (True, None)
    resolved, error = check_time(datetime(2999, 1, 1, tzinfo=timezone.utc))
    assert not resolved
    assert error is not None and error.startswith("Waiting until")


def test_check_time_accepts_yaml_date_ref() -> None:
    assert check_time(date(2020, 1, 1)) == (True, None)
    assert check_time(date(2999, 1, 1))[0] is False


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def test_cleanup_merged_worktrees_preserves_fresh_worktrees(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    wt = get_worktrees_dir(repo) / "feature"
    _git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    # `git branch --merged` prints a branch checked out elsewhere as "+ feature".
    assert "+ feature" in _git(repo, "branch", "--merged", "master")

    # A fresh clean worktree is reachable from master, but may be in active use.
    assert cleanup_merged_worktrees(repo, base_branch="master") == 0
    assert wt.exists()
    assert "feature" in _git(repo, "branch", "--list", "feature")
