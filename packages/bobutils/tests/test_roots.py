"""Tests for bobutils.roots."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from bobutils.roots import find_repo_root


def test_find_repo_root_returns_path_with_git(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    result = find_repo_root(tmp_path)
    assert result == tmp_path.resolve()


def test_find_repo_root_subdirectory(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subdir = tmp_path / "a" / "b"
    subdir.mkdir(parents=True)
    result = find_repo_root(subdir)
    assert result == tmp_path.resolve()


def test_find_repo_root_raises_outside_git(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="git rev-parse"):
        find_repo_root(tmp_path)


def test_find_repo_root_wraps_missing_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_missing_git(
        *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", raise_missing_git)

    with pytest.raises(RuntimeError, match="git executable not found"):
        find_repo_root(tmp_path)


def test_find_repo_root_worktree_returns_worktree_root(tmp_path: Path) -> None:
    """Worktree root differs from shared-state root — document the contrast."""
    base = tmp_path / "base"
    base.mkdir()
    subprocess.run(
        ["git", "init"],
        cwd=base,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "-b", "main"],
        cwd=base,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test"], cwd=base, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=base, capture_output=True
    )
    # Commit something so worktree add can branch.
    # Use --no-verify to bypass any workspace-level commit hooks.
    (base / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=base, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "--no-verify", "-m", "init"],
        cwd=base,
        check=True,
        capture_output=True,
    )
    wt = tmp_path / "worktree"
    subprocess.run(
        ["git", "worktree", "add", str(wt), "-b", "wt-branch"],
        cwd=base,
        check=True,
        capture_output=True,
    )
    # find_repo_root resolves to the *worktree* root, not the base
    assert find_repo_root(wt) == wt.resolve()
