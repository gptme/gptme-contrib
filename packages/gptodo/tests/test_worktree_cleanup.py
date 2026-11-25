"""Cleanup of merged worktrees must require evidence of inactivity.

Regression: stripping only the ``* `` marker from ``git branch --merged``
output made ``cleanup_merged_worktrees`` a no-op for every gptodo worktree.
Stripping ``+ `` as well then deleted a worktree that had just been created
from the base branch and was still in active use (gptme/gptme-contrib#2474).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from gptodo.worktree import cleanup_merged_worktrees


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _add_worktree(repo: Path, name: str, branch: str) -> Path:
    wt = repo / ".worktrees" / name
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", str(wt), "-b", branch, "master")
    return wt


def _age_tracked_files(worktree: Path, days: float) -> None:
    old = time.time() - days * 86400
    for path in worktree.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            os.utime(path, (old, old))


def test_fresh_worktree_is_preserved(tmp_path: Path) -> None:
    """A worktree created from master is merged but may be in active use."""
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, "fresh", "feature")

    # A branch checked out in a linked worktree is printed as "+ feature".
    assert "+ feature" in _git(repo, "branch", "--merged", "master")

    assert cleanup_merged_worktrees(repo, base_branch="master") == 0
    assert wt.exists()
    assert "feature" in _git(repo, "branch", "--list", "feature")


def test_idle_merged_worktree_is_removed(tmp_path: Path) -> None:
    """Merged, clean and quiet for longer than idle_days => eligible."""
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, "stale", "feature")
    _age_tracked_files(wt, days=30)

    assert cleanup_merged_worktrees(repo, base_branch="master", idle_days=7) == 1
    assert not wt.exists()
    assert "feature" not in _git(repo, "branch", "--list", "feature")


def test_dirty_worktree_is_preserved(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, "dirty", "feature")
    _age_tracked_files(wt, days=30)
    (wt / "scratch.txt").write_text("work in progress\n")

    assert cleanup_merged_worktrees(repo, base_branch="master", idle_days=7) == 0
    assert wt.exists()


def test_locked_worktree_is_preserved(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, "locked", "feature")
    _age_tracked_files(wt, days=30)
    _git(repo, "worktree", "lock", str(wt))

    assert cleanup_merged_worktrees(repo, base_branch="master", idle_days=7) == 0
    assert wt.exists()


def test_unmerged_worktree_is_preserved(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, "unmerged", "feature")
    (wt / "new.txt").write_text("unmerged work\n")
    _git(wt, "add", "new.txt")
    _git(wt, "commit", "-m", "feat: unmerged")
    _age_tracked_files(wt, days=30)

    assert cleanup_merged_worktrees(repo, base_branch="master", idle_days=7) == 0
    assert wt.exists()


def test_recently_active_merged_worktree_is_preserved(tmp_path: Path) -> None:
    """Old files are not enough: a single recent tracked change means active."""
    repo = _make_repo(tmp_path)
    wt = _add_worktree(repo, "recent", "feature")
    _age_tracked_files(wt, days=30)
    touched = wt / "README.md"
    now = time.time()
    os.utime(touched, (now, now))

    assert cleanup_merged_worktrees(repo, base_branch="master", idle_days=7) == 0
    assert wt.exists()
