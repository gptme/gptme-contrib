"""create-pr.py: commits the whole index and leaves state that blocks retries."""

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "gptme_lessons_extras"
    / "create-pr.py"
)

LESSON = """---
match:
  keywords: [git]
---
# Use git carefully

## Rule
r
## Context
c
## Failure Signals
f
## Anti-pattern
a
## Recommended Pattern
p
## Fix Recipe
x
## Rationale
y
## Verification Checklist
v
## Origin
o
"""


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text("#!/bin/sh\necho https://github.com/o/r/pull/1\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "master")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    git(r, "config", "commit.gpgsign", "false")
    (r / "README").write_text("x\n")
    git(r, "add", "README")
    git(r, "commit", "-q", "-m", "init")
    git(r, "remote", "add", "origin", str(remote))
    git(r, "push", "-q", "origin", "master")
    (r / "lessons" / "tools").mkdir(parents=True)
    (r / "lessons" / "tools" / "git-careful.md").write_text(LESSON)
    monkeypatch.chdir(r)
    return r


@pytest.fixture
def create_pr():
    spec = importlib.util.spec_from_file_location("create_pr_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pr_commit_contains_only_the_lesson(repo, create_pr):
    # The user has unrelated work staged in the index (e.g. half-done refactor).
    (repo / "secret-wip.txt").write_text("unrelated staged work\n")
    git(repo, "add", "secret-wip.txt")

    ok = create_pr.create_lesson_pr(repo / "lessons" / "tools" / "git-careful.md")
    assert ok
    files = git(
        repo, "show", "--name-only", "--format=", "lesson/tools-git-careful"
    ).split()
    assert files == ["lessons/tools/git-careful.md"], f"PR commit also pushed: {files}"
    assert git(repo, "diff", "--cached", "--name-only").split() == ["secret-wip.txt"]


def test_failed_run_leaves_lesson_file_in_working_tree(repo, create_pr):
    lesson = repo / "lessons" / "tools" / "git-careful.md"
    git(repo, "remote", "set-url", "origin", str(repo.parent / "missing.git"))
    assert create_pr.create_lesson_pr(lesson) is False  # transient push failure
    # The "failed" run committed the lesson on a side branch and checked out
    # master again, which deletes the untracked-on-master file from disk.
    assert (
        lesson.exists()
    ), "lesson file vanished from the working tree after a failed run"


def test_retry_after_failed_push_succeeds(repo, create_pr):
    lesson = repo / "lessons" / "tools" / "git-careful.md"
    git(repo, "remote", "set-url", "origin", str(repo.parent / "missing.git"))
    assert create_pr.create_lesson_pr(lesson) is False  # transient push failure

    git(repo, "remote", "set-url", "origin", str(repo.parent / "remote.git"))
    lesson.parent.mkdir(parents=True, exist_ok=True)
    lesson.write_text(LESSON)  # restore the file so only the branch state matters
    assert (
        create_pr.create_lesson_pr(lesson) is True
    ), "retry blocked by leftover branch"


def test_failed_run_restores_branch_index_and_edits(repo, create_pr):
    lesson = repo / "lessons" / "tools" / "git-careful.md"
    (repo / "wip.txt").write_text("staged\n")
    git(repo, "add", "wip.txt")
    git(repo, "remote", "set-url", "origin", str(repo.parent / "missing.git"))

    assert create_pr.create_lesson_pr(lesson) is False

    assert git(repo, "branch", "--show-current").strip() == "master"
    assert git(repo, "branch", "--list", "lesson/*") == ""
    assert git(repo, "diff", "--cached", "--name-only").split() == ["wip.txt"]
    assert lesson.read_text() == LESSON


def test_existing_branch_is_not_deleted(repo, create_pr):
    git(repo, "branch", "lesson/tools-git-careful")
    lesson = repo / "lessons" / "tools" / "git-careful.md"

    assert create_pr.create_lesson_pr(lesson) is False

    assert git(repo, "branch", "--list", "lesson/*").strip()
    assert lesson.exists()


def test_detached_head_is_restored_after_failure(repo, create_pr):
    git(repo, "checkout", "-q", "--detach")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "remote", "set-url", "origin", str(repo.parent / "missing.git"))
    lesson = repo / "lessons" / "tools" / "git-careful.md"

    assert create_pr.create_lesson_pr(lesson) is False

    assert git(repo, "branch", "--show-current").strip() == ""
    assert git(repo, "rev-parse", "HEAD") == head
    assert lesson.exists()


def test_gh_failure_after_push_keeps_lesson_and_says_how_to_finish(
    repo, create_pr, capsys
):
    (repo.parent / "bin" / "gh").write_text("#!/bin/sh\nexit 1\n")
    lesson = repo / "lessons" / "tools" / "git-careful.md"

    assert create_pr.create_lesson_pr(lesson) is False

    assert lesson.exists()
    assert git(repo, "branch", "--list", "lesson/*") == ""
    assert git(repo, "ls-remote", "--heads", "origin", "lesson/*")
    assert "gh pr create --head lesson/tools-git-careful" in capsys.readouterr().out


def test_relative_lesson_path(repo, create_pr):
    assert create_pr.create_lesson_pr(Path("lessons/tools/git-careful.md")) is True
    files = git(
        repo, "show", "--name-only", "--format=", "lesson/tools-git-careful"
    ).split()
    assert files == ["lessons/tools/git-careful.md"]
