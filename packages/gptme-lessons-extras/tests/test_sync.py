"""Regression tests for network sync (#2362 E7, E8)."""

import importlib
import os
import subprocess
import sys
import types
from pathlib import Path

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


def test_init_repo_reports_why_git_refused_a_valid_clone(tmp_path, monkeypatch, sync):
    remote = _bare_remote_with_seed(tmp_path)
    clone = tmp_path / "network"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(clone)],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")

    with pytest.raises(sync.SyncError, match="dubious ownership"):
        sync.init_repo(clone)


def test_init_repo_refuses_directory_inside_enclosing_repo(tmp_path, monkeypatch, sync):
    # e.g. ~/.gptme/network inside a version-controlled home: accepting it would
    # let push_lessons push the enclosing repo's commits.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    target = tmp_path / "network"
    target.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))

    with pytest.raises(sync.SyncError, match="inside the work tree"):
        sync.init_repo(target)


def test_init_repo_accepts_existing_clone_through_symlink(tmp_path, sync):
    remote = _bare_remote_with_seed(tmp_path)
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(clone)],
        check=True,
        capture_output=True,
    )
    link = tmp_path / "network"
    link.symlink_to(clone, target_is_directory=True)

    assert sync.init_repo(link) == link


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


def test_init_repo_force_replaces_symlink_and_keeps_its_target(
    tmp_path, monkeypatch, sync
):
    monkeypatch.setattr(sync, "NETWORK_REPO_URL", str(_bare_remote_with_seed(tmp_path)))
    real = tmp_path / "real"
    real.mkdir()
    (real / "my-notes.md").write_text("user content\n")
    link = tmp_path / "network"
    link.symlink_to(real, target_is_directory=True)

    assert sync.init_repo(link, force=True) == link
    assert not link.is_symlink()
    assert (link / "README.md").exists()
    assert (real / "my-notes.md").read_text() == "user content\n"


def test_init_repo_force_restores_directory_when_swap_fails(
    tmp_path, monkeypatch, sync
):
    monkeypatch.setattr(sync, "NETWORK_REPO_URL", str(_bare_remote_with_seed(tmp_path)))
    target = tmp_path / "network"
    target.mkdir()
    (target / "my-notes.md").write_text("user content\n")
    before = sorted(p.name for p in tmp_path.iterdir())
    real_rename = Path.rename

    def fail_moving_clone_in(self, dest):
        if self.name == "network" and self != target:
            raise PermissionError("rename refused")
        return real_rename(self, dest)

    monkeypatch.setattr(Path, "rename", fail_moving_clone_in)

    with pytest.raises(sync.SyncError, match="rename refused"):
        sync.init_repo(target, force=True)
    assert (target / "my-notes.md").read_text() == "user content\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_init_repo_force_keeps_original_when_restore_fails(tmp_path, monkeypatch, sync):
    # If the clone cannot be moved into place *and* the restore fails too, the
    # original directory must survive somewhere (and the error must say where)
    # rather than being deleted by the staging cleanup.
    monkeypatch.setattr(sync, "NETWORK_REPO_URL", str(_bare_remote_with_seed(tmp_path)))
    target = tmp_path / "network"
    target.mkdir()
    (target / "my-notes.md").write_text("user content\n")
    real_rename = Path.rename

    def fail_swap_and_restore(self, dest):
        if self.name in ("network", "old") and self != target:
            raise PermissionError("rename refused")
        return real_rename(self, dest)

    monkeypatch.setattr(Path, "rename", fail_swap_and_restore)

    with pytest.raises(sync.SyncError, match="could not restore"):
        sync.init_repo(target, force=True)
    kept = list(tmp_path.rglob("my-notes.md"))
    assert len(kept) == 1
    assert kept[0].read_text() == "user content\n"


def test_init_repo_force_keeps_original_on_interrupt_between_renames(
    tmp_path, monkeypatch, sync
):
    # A BaseException that is not an OSError (the CLI exits 130 on
    # KeyboardInterrupt) between parking the old dir and moving the clone in
    # must not let the staging cleanup delete the user's only copy.
    monkeypatch.setattr(sync, "NETWORK_REPO_URL", str(_bare_remote_with_seed(tmp_path)))
    target = tmp_path / "network"
    target.mkdir()
    (target / "my-notes.md").write_text("user content\n")
    real_rename = Path.rename

    def interrupt_moving_clone_in(self, dest):
        if self.name == "network" and self != target:
            raise KeyboardInterrupt
        return real_rename(self, dest)

    monkeypatch.setattr(Path, "rename", interrupt_moving_clone_in)

    with pytest.raises(KeyboardInterrupt):
        sync.init_repo(target, force=True)
    kept = list(tmp_path.rglob("my-notes.md"))
    assert len(kept) == 1
    assert kept[0].read_text() == "user content\n"


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
