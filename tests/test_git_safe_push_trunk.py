"""Tests for git-safe-push-master / git-safe-pull trunk derivation.

The regression this file exists to prevent: the scripts used to hardcode the
trunk branch to ``master`` (``origin/master``, ``refs/heads/master``, and a
``CURRENT_BRANCH != master`` guard). A fork whose default branch is ``main``
(GitHub's default since 2020) would target a non-existent ``origin/master`` and
refuse to run. These tests build ``main``-default fixture repos and assert the
scripts derive the trunk instead of assuming ``master``.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SAFE_PUSH = REPO_ROOT / "scripts" / "git" / "git-safe-push-master"
SAFE_PULL = REPO_ROOT / "scripts" / "git" / "git-safe-pull"


def _git(*args: str, cwd: Path, **kw) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, **kw)


def _init_repo(path: Path, branch: str) -> None:
    _git("init", "-b", branch, str(path), cwd=path.parent, check=True)
    _git("config", "user.email", "test@test.com", cwd=path, check=True)
    _git("config", "user.name", "Test", cwd=path, check=True)
    # Neutralize any global hooks (e.g. identity allowlists) for the fixture.
    _git("config", "core.hooksPath", "/dev/null", cwd=path, check=True)


def _commit(path: Path, name: str, content: str, msg: str) -> None:
    (path / name).write_text(content)
    _git("add", name, cwd=path, check=True)
    _git("commit", "-m", msg, cwd=path, check=True)


@pytest.fixture
def remote_and_clone(tmp_path: Path):
    """A bare remote on the given default branch + a clone with origin/HEAD set.

    Returns a factory: ``make(branch) -> clone_path``.
    """

    def make(branch: str) -> Path:
        bare = tmp_path / f"remote-{branch}.git"
        _git("init", "--bare", "-b", branch, str(bare), cwd=tmp_path, check=True)

        seed = tmp_path / f"seed-{branch}"
        seed.mkdir()
        _init_repo(seed, branch)
        _commit(seed, "README.md", "init\n", "init")
        _git("remote", "add", "origin", str(bare), cwd=seed, check=True)
        _git("push", "-u", "origin", branch, cwd=seed, check=True)

        clone = tmp_path / f"clone-{branch}"
        _git("clone", str(bare), str(clone), cwd=tmp_path, check=True)
        _git("config", "user.email", "test@test.com", cwd=clone, check=True)
        _git("config", "user.name", "Test", cwd=clone, check=True)
        _git("config", "core.hooksPath", "/dev/null", cwd=clone, check=True)
        return clone

    return make


def _run(script: Path, cwd: Path, env_extra: dict | None = None):
    env = os.environ.copy()
    env["GIT_SAFE_PUSH_LOCK_TIMEOUT"] = "10"
    env["GIT_SAFE_PULL_LOCK_TIMEOUT"] = "10"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [str(script)], cwd=cwd, capture_output=True, text=True, env=env
    )


def _remote_head(clone: Path, branch: str) -> str:
    return _git("rev-parse", f"origin/{branch}", cwd=clone).stdout.strip()


def test_scripts_exist_and_executable():
    for s in (SAFE_PUSH, SAFE_PULL):
        assert s.exists()
        assert os.access(s, os.X_OK)


def test_push_derives_main_trunk(remote_and_clone):
    """On a main-default clone, push publishes to origin/main (not origin/master)."""
    clone = remote_and_clone("main")
    before = _remote_head(clone, "main")
    _commit(clone, "feature.txt", "work\n", "feat: add feature")

    res = _run(SAFE_PUSH, clone)
    assert res.returncode == 0, res.stderr
    after = _remote_head(clone, "main")
    assert after != before, "origin/main did not advance"
    # Local main now matches what we pushed.
    local = _git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    assert local == after
    assert "master" not in res.stdout, res.stdout


def test_push_still_works_on_master_default(remote_and_clone):
    """Back-compat: a master-default repo still publishes to origin/master."""
    clone = remote_and_clone("master")
    before = _remote_head(clone, "master")
    _commit(clone, "feature.txt", "work\n", "feat: add feature")

    res = _run(SAFE_PUSH, clone)
    assert res.returncode == 0, res.stderr
    assert _remote_head(clone, "master") != before


def test_push_guard_names_derived_trunk(remote_and_clone):
    """Running on a non-trunk branch errors, naming the derived trunk."""
    clone = remote_and_clone("main")
    _git("checkout", "-b", "feature", cwd=clone, check=True)
    res = _run(SAFE_PUSH, clone)
    assert res.returncode == 2
    assert "main" in res.stderr
    assert "feature" in res.stderr


def test_push_trunk_env_override(remote_and_clone):
    """GIT_SAFE_TRUNK overrides derivation."""
    clone = remote_and_clone("main")
    # HEAD is on main; force the guard to expect a different (wrong) trunk.
    res = _run(SAFE_PUSH, clone, {"GIT_SAFE_TRUNK": "master"})
    assert res.returncode == 2
    assert "master" in res.stderr  # guard uses the overridden trunk name


def test_pull_fast_forwards_on_main(remote_and_clone):
    """git-safe-pull fast-forwards a behind main-default clone."""
    clone = remote_and_clone("main")
    # Advance origin/main from a second clone.
    other = _git("rev-parse", "--show-toplevel", cwd=clone).stdout.strip()
    other_clone = Path(other).parent / "other"
    _git(
        "clone",
        str(Path(other).parent / "remote-main.git"),
        str(other_clone),
        cwd=Path(other).parent,
        check=True,
    )
    _git("config", "user.email", "t@t.com", cwd=other_clone, check=True)
    _git("config", "user.name", "T", cwd=other_clone, check=True)
    _git("config", "core.hooksPath", "/dev/null", cwd=other_clone, check=True)
    _commit(other_clone, "upstream.txt", "up\n", "feat: upstream change")
    _git("push", "origin", "main", cwd=other_clone, check=True)

    res = _run(SAFE_PULL, clone)
    assert res.returncode == 0, res.stderr
    assert (clone / "upstream.txt").exists(), "fast-forward did not land upstream file"
