"""Tests for generic collectors (no network — only filesystem/git operations)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gptme_daily_briefing.collectors import (
    collect_recent_highlights,
    collect_waiting_tasks,
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_workspace(tmp_path: Path) -> Path:
    """A fresh git repo with a couple of commits, so highlights have data.

    Uses a non-master default branch to dodge any global hook that blocks
    direct commits to master in unfamiliar repos.
    """
    _git("init", "-q", "-b", "test-default", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    _git("config", "commit.gpgsign", "false", cwd=tmp_path)
    (tmp_path / "README.md").write_text("# Test\n")
    _git("add", "README.md", cwd=tmp_path)
    _git("commit", "-q", "--no-verify", "-m", "feat: first commit", cwd=tmp_path)
    (tmp_path / "README.md").write_text("# Test\n\n## Section\n")
    _git("commit", "-aq", "--no-verify", "-m", "docs: add section", cwd=tmp_path)
    (tmp_path / "README.md").write_text("# Test\n\n## Section\n\nBody\n")
    _git("commit", "-aq", "--no-verify", "-m", "fix: trailing body", cwd=tmp_path)
    return tmp_path


def test_collect_recent_highlights_returns_subjects_in_order(git_workspace: Path) -> None:
    # `origin/master` doesn't exist in the fixture repo; the function falls back to local log
    out = collect_recent_highlights(git_workspace, limit=3)
    assert out == ["fix: trailing body", "docs: add section", "feat: first commit"]


def test_collect_recent_highlights_respects_limit(git_workspace: Path) -> None:
    out = collect_recent_highlights(git_workspace, limit=1)
    assert out == ["fix: trailing body"]


def test_collect_recent_highlights_empty_repo(tmp_path: Path) -> None:
    # No git repo at all — should return [] without raising
    out = collect_recent_highlights(tmp_path, limit=5)
    assert out == []


def test_collect_waiting_tasks_parses_frontmatter(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    # Quote the value because '#' would otherwise start a YAML comment
    (tasks / "blocked-on-erik.md").write_text(
        "---\n"
        "state: waiting\n"
        'waiting_for: "Erik to merge PR #123"\n'
        "created: 2026-04-29T00:00:00+00:00\n"
        "---\n"
        "# A task\n"
    )
    (tasks / "active.md").write_text(
        "---\n" "state: active\n" "created: 2026-04-29T00:00:00+00:00\n" "---\n" "# Another task\n"
    )
    (tasks / "waiting-no-blocker.md").write_text(
        "---\nstate: waiting\ncreated: 2026-04-29T00:00:00+00:00\n---\n"
    )
    (tasks / "no-frontmatter.md").write_text("Just a body.\n")

    out = collect_waiting_tasks(tmp_path)

    assert out == [{"task": "blocked-on-erik", "waiting_for": "Erik to merge PR #123"}]


def test_collect_waiting_tasks_truncates_long_blocker(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    big = "x" * 500
    (tasks / "long.md").write_text(
        "---\n"
        "state: waiting\n"
        f"waiting_for: {big}\n"
        "created: 2026-04-29T00:00:00+00:00\n"
        "---\n"
    )
    out = collect_waiting_tasks(tmp_path)
    assert len(out) == 1
    assert len(out[0]["waiting_for"]) == 200


def test_collect_waiting_tasks_no_tasks_dir(tmp_path: Path) -> None:
    # Workspace exists but no tasks/ subdir
    assert collect_waiting_tasks(tmp_path) == []


def test_collect_waiting_tasks_respects_limit(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    for i in range(12):
        (tasks / f"w{i:02d}.md").write_text(
            "---\n"
            "state: waiting\n"
            f"waiting_for: blocker {i}\n"
            "created: 2026-04-29T00:00:00+00:00\n"
            "---\n"
        )
    out = collect_waiting_tasks(tmp_path, limit=4)
    assert len(out) == 4
    # Sorted alphabetically by stem
    assert [t["task"] for t in out] == ["w00", "w01", "w02", "w03"]
