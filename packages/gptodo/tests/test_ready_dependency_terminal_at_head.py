"""A dependency that is `done` only in the working tree must not release its dependent.

A session that times out can leave a task file at `state: done` uncommitted in a
shared checkout, next to its uncommitted work. Readiness must check HEAD for a
dirty terminal dependency; clean files and non-git trees keep disk semantics.
"""

import subprocess
from pathlib import Path

import pytest

from gptodo import utils
from gptodo.utils import is_task_ready, load_tasks

DEPENDENT = (
    "---\nstate: backlog\ncreated: 2026-11-23T00:00:00+00:00\ndepends: [prereq]\n---\n# Dependent\n"
)


def _prereq(state: str) -> str:
    return f"---\nstate: {state}\ncreated: 2026-11-23T00:00:00+00:00\n---\n# Prereq\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture(autouse=True)
def _fresh_cache():
    utils._dirty_task_cache.clear()
    yield
    utils._dirty_task_cache.clear()


def _ready(tasks_dir: Path) -> bool:
    tasks = {t.name: t for t in load_tasks(tasks_dir)}
    return is_task_ready(tasks["dependent"], tasks)


def _repo(tmp_path: Path, committed_state: str) -> Path:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "dependent.md").write_text(DEPENDENT)
    (tasks_dir / "prereq.md").write_text(_prereq(committed_state))
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "init",
    )
    return tasks_dir


def test_done_only_in_working_tree_blocks(tmp_path):
    tasks_dir = _repo(tmp_path, "active")
    (tasks_dir / "prereq.md").write_text(_prereq("done"))
    assert not _ready(tasks_dir)


def test_done_at_head_releases(tmp_path):
    tasks_dir = _repo(tmp_path, "done")
    assert _ready(tasks_dir)


def test_dirty_but_terminal_at_head_releases(tmp_path):
    tasks_dir = _repo(tmp_path, "done")
    (tasks_dir / "prereq.md").write_text(_prereq("done") + "\nextra note\n")
    assert _ready(tasks_dir)


def test_uncommitted_new_done_dependency_blocks(tmp_path):
    tasks_dir = _repo(tmp_path, "done")
    (tasks_dir / "prereq.md").unlink()
    _git(tmp_path, "rm", "-q", "--cached", "tasks/prereq.md")
    _git(
        tmp_path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "drop",
    )
    (tasks_dir / "prereq.md").write_text(_prereq("done"))
    # untracked files are not in `git diff HEAD`, so disk semantics apply
    assert _ready(tasks_dir)


def test_outside_git_keeps_disk_semantics(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "dependent.md").write_text(DEPENDENT)
    (tasks_dir / "prereq.md").write_text(_prereq("done"))
    assert _ready(tasks_dir)
