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


def test_cached_clean_dependency_changed_to_done_still_blocks(tmp_path):
    tasks_dir = _repo(tmp_path, "active")
    assert utils._dirty_task_names(tasks_dir) == frozenset()
    (tasks_dir / "prereq.md").write_text(_prereq("done"))
    assert not _ready(tasks_dir)


def test_done_at_head_releases(tmp_path):
    tasks_dir = _repo(tmp_path, "done")
    assert _ready(tasks_dir)


@pytest.mark.parametrize("state_line", ['"state": done', 'state: "done"'])
def test_head_state_uses_frontmatter_parser(tmp_path, state_line):
    tasks_dir = _repo(tmp_path, "done")
    path = tasks_dir / "prereq.md"
    path.write_text(_prereq("done").replace("state: done", state_line))
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
        "quoted state",
    )
    path.write_text(_prereq("done") + "\nextra note\n")
    assert _ready(tasks_dir)


def test_dirty_but_terminal_at_head_releases(tmp_path):
    tasks_dir = _repo(tmp_path, "done")
    (tasks_dir / "prereq.md").write_text(_prereq("done") + "\nextra note\n")
    assert _ready(tasks_dir)


@pytest.mark.parametrize("operation", ["diff", "show"])
@pytest.mark.parametrize("failure", ["timeout", "error", "exit"])
def test_git_check_failure_does_not_release_dependency(tmp_path, monkeypatch, operation, failure):
    tasks_dir = _repo(tmp_path, "active")
    (tasks_dir / "prereq.md").write_text(_prereq("done"))
    run = subprocess.run

    def failing_run(args, **kwargs):
        if args[3] == operation:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 10)
            if failure == "error":
                raise OSError("git unavailable")
            return subprocess.CompletedProcess(args, 128, "", "fatal: cannot read repository")
        return run(args, **kwargs)

    monkeypatch.setattr(utils.subprocess, "run", failing_run)
    assert not _ready(tasks_dir)


def test_untracked_done_dependency_keeps_disk_semantics(tmp_path):
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


@pytest.mark.parametrize("committed_state", ["active", "done", "cancelled"])
def test_staged_archive_rename_checks_original_head_state(tmp_path, committed_state):
    tasks_dir = _repo(tmp_path, committed_state)
    archive = tasks_dir / "archive"
    archive.mkdir()
    _git(tmp_path, "mv", "tasks/prereq.md", "tasks/archive/prereq.md")
    # Keep enough unchanged content for rename detection even when the state changes.
    (archive / "prereq.md").write_text(_prereq("done"))
    tasks = utils.build_dependency_universe({t.name: t for t in load_tasks(tasks_dir)}, tasks_dir)
    assert is_task_ready(tasks["dependent"], tasks) is (committed_state in ("done", "cancelled"))


def test_unborn_head_keeps_disk_semantics(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "dependent.md").write_text(DEPENDENT)
    (tasks_dir / "prereq.md").write_text(_prereq("done"))
    _git(tmp_path, "init", "-q")
    # A fresh repository has no HEAD, so every file is untracked: the documented
    # untracked-file disk semantics apply instead of blocking forever.
    assert _ready(tasks_dir)


def test_outside_git_keeps_disk_semantics(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "dependent.md").write_text(DEPENDENT)
    (tasks_dir / "prereq.md").write_text(_prereq("done"))
    assert _ready(tasks_dir)
