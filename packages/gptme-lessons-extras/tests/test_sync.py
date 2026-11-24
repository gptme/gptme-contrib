"""Regression tests for network sync (#2362 E7, E8)."""

import importlib
import os
import subprocess
import sys
import types

import pytest


@pytest.fixture
def sync(monkeypatch):
    # sync.py imports `lessons.import` through the src/lessons symlink; stub it
    # so the tests do not depend on that layout.
    pkg = types.ModuleType("lessons")
    pkg.__path__ = []
    mod = types.ModuleType("lessons.import")
    mod.review_network_lesson = lambda *a, **k: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "lessons", pkg)
    monkeypatch.setitem(sys.modules, "lessons.import", mod)
    monkeypatch.delitem(sys.modules, "gptme_lessons_extras.sync", raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    return importlib.import_module("gptme_lessons_extras.sync")


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _bare_remote_with_seed(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "master", str(remote)], check=True
    )
    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(seed)], check=True, capture_output=True
    )
    for k, v in (
        ("user.email", "t@e.com"),
        ("user.name", "t"),
        ("commit.gpgsign", "false"),
    ):
        _git(seed, "config", k, v)
    (seed / "README.md").write_text("network\n")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-q", "-m", "seed")
    _git(seed, "push", "-q", "-u", "origin", "master")
    return remote


def test_init_repo_refuses_non_repo_directory_without_force(
    tmp_path, monkeypatch, sync
):
    target = tmp_path / "network"
    target.mkdir()
    (target / "my-notes.md").write_text("user content\n")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setattr(sync, "NETWORK_REPO_URL", str(_bare_remote_with_seed(tmp_path)))

    with pytest.raises(sync.SyncError, match="not a git repository"):
        sync.init_repo(target)
    assert (target / "my-notes.md").read_text() == "user content\n"


def test_init_repo_force_keeps_directory_when_clone_fails(tmp_path, monkeypatch, sync):
    target = tmp_path / "network"
    target.mkdir()
    (target / "my-notes.md").write_text("user content\n")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setattr(sync, "NETWORK_REPO_URL", str(tmp_path / "offline.git"))

    with pytest.raises(sync.SyncError):
        sync.init_repo(target, force=True)
    assert (target / "my-notes.md").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["network"]


def test_init_repo_force_replaces_directory_after_successful_clone(
    tmp_path, monkeypatch, sync
):
    monkeypatch.setattr(sync, "NETWORK_REPO_URL", str(_bare_remote_with_seed(tmp_path)))
    target = tmp_path / "network"
    target.mkdir()
    (target / "stale.md").write_text("old\n")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))

    assert sync.init_repo(target, force=True) == target
    assert (target / "README.md").read_text() == "network\n"
    assert not (target / "stale.md").exists()


def test_push_lessons_pushes_commit_left_by_failed_push(tmp_path, monkeypatch, sync):
    remote = _bare_remote_with_seed(tmp_path)
    net = tmp_path / "net"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(net)], check=True, capture_output=True
    )
    for k, v in (
        ("user.email", "t@e.com"),
        ("user.name", "t"),
        ("commit.gpgsign", "false"),
    ):
        _git(net, "config", k, v)

    (net / "bob").mkdir()
    (net / "bob" / "lesson.md").write_text("exported\n")
    _git(net, "remote", "set-url", "origin", str(tmp_path / "offline.git"))
    with pytest.raises(sync.SyncError):
        sync.push_lessons(net)  # commit succeeds, push fails

    _git(net, "remote", "set-url", "origin", str(remote))
    assert sync.push_lessons(net) is True  # next sync, export unchanged
    assert (
        "bob/lesson.md"
        in _git(remote, "ls-tree", "-r", "--name-only", "master").split()
    )
    # Nothing left to push afterwards
    assert sync.push_lessons(net) is False
