"""Tests for git-safe-push-master / git-safe-pull trunk derivation.

The regression this file exists to prevent: the scripts used to hardcode the
trunk branch to ``master`` (``origin/master``, ``refs/heads/master``, and a
``CURRENT_BRANCH != master`` guard). A fork whose default branch is ``main``
(GitHub's default since 2020) would target a non-existent ``origin/master`` and
refuse to run. These tests build ``main``-default fixture repos and assert the
scripts derive the trunk instead of assuming ``master``.
"""

import os
import shutil
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


def _run(
    script: Path,
    cwd: Path,
    env_extra: dict | None = None,
    args: tuple[str, ...] = (),
):
    env = os.environ.copy()
    env["GIT_SAFE_PUSH_LOCK_TIMEOUT"] = "10"
    env["GIT_SAFE_PULL_LOCK_TIMEOUT"] = "10"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [str(script), *args], cwd=cwd, capture_output=True, text=True, env=env
    )


def _remote_head(clone: Path, branch: str) -> str:
    return _git("rev-parse", f"origin/{branch}", cwd=clone).stdout.strip()


def _bare_head(clone: Path, branch: str) -> str:
    """SHA of the fixture's bare remote branch, bypassing the clone's tracking ref."""
    bare = clone.parent / f"remote-{branch}.git"
    return _git("rev-parse", f"refs/heads/{branch}", cwd=bare).stdout.strip()


def _rejecting_path(tmp_path: Path) -> tuple[str, Path]:
    """PATH with git/flock wrappers that log their argv and refuse to run."""
    bin_dir = tmp_path / "reject-bin"
    bin_dir.mkdir()
    log = tmp_path / "reject.log"
    for name in ("git", "flock"):
        wrapper = bin_dir / name
        wrapper.write_text(f'#!/bin/bash\necho "{name} $*" >> "{log}"\nexit 97\n')
        wrapper.chmod(0o755)
    return f"{bin_dir}{os.pathsep}{os.environ['PATH']}", log


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


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_prints_usage_and_exits_zero_without_publishing(remote_and_clone, flag):
    """Help is a pure read: exit 0, Usage on stdout, remote untouched."""
    clone = remote_and_clone("main")
    _commit(clone, "feature.txt", "work\n", "feat: unpublished")
    before = _bare_head(clone, "main")

    res = _run(SAFE_PUSH, clone, args=(flag,))
    assert res.returncode == 0, res.stderr
    assert "Usage" in res.stdout, res.stdout
    assert _bare_head(clone, "main") == before


@pytest.mark.parametrize("arg", ["--bogus", "main", "--force"])
def test_unknown_args_print_usage_and_exit_two_without_publishing(
    remote_and_clone, arg
):
    """Unknown or positional args are rejected: exit 2, Usage on stderr, remote untouched."""
    clone = remote_and_clone("main")
    _commit(clone, "feature.txt", "work\n", "feat: unpublished")
    before = _bare_head(clone, "main")

    res = _run(SAFE_PUSH, clone, args=(arg,))
    assert res.returncode == 2, res.stdout
    assert "Usage" in res.stderr, res.stderr
    assert _bare_head(clone, "main") == before


@pytest.mark.parametrize(
    ("args", "expected_rc"),
    [
        (("-h",), 0),
        (("--help",), 0),
        (("--bogus",), 2),
        (("main",), 2),
        (("--force",), 2),
    ],
)
def test_help_and_unknown_args_never_invoke_git_or_flock(tmp_path, args, expected_rc):
    """Argument handling runs before any git/flock call, so it works outside a repo."""
    path, log = _rejecting_path(tmp_path)
    res = _run(SAFE_PUSH, tmp_path, env_extra={"PATH": path}, args=args)
    assert res.returncode == expected_rc, res.stderr
    assert "Usage" in (res.stdout + res.stderr)
    assert not log.exists(), f"git/flock was invoked: {log.read_text()}"


FORBIDDEN_PUBLISH = {"commit-tree", "push", "update-ref"}


def _git_spy(tmp_path: Path) -> tuple[str, Path]:
    """PATH with a git wrapper that logs each subcommand, refuses the publish/ref-write
    ones, and passes everything else through to the real git."""
    real_git = shutil.which("git")
    bin_dir = tmp_path / "spy-bin"
    bin_dir.mkdir()
    log = tmp_path / "spy.log"
    wrapper = bin_dir / "git"
    wrapper.write_text(
        "#!/bin/bash\n"
        f'echo "$1" >> "{log}"\n'
        'case "$1" in commit-tree|push|update-ref) exit 97 ;; esac\n'
        f'exec "{real_git}" "$@"\n'
    )
    wrapper.chmod(0o755)
    return f"{bin_dir}{os.pathsep}{os.environ['PATH']}", log


def _git_subcommands(log: Path) -> set[str]:
    return set(log.read_text().split()) if log.exists() else set()


def _local_state(clone: Path) -> tuple[str, str, str]:
    """HEAD, index entries, and worktree status: what a dry run must not change."""
    return (
        _git("rev-parse", "HEAD", cwd=clone).stdout,
        _git("ls-files", "-s", cwd=clone).stdout,
        _git("status", "--porcelain", cwd=clone).stdout,
    )


def _advance_remote(clone: Path, branch: str, name: str, content: str) -> None:
    """Publish a commit to the fixture remote from a second clone, not ``clone``."""
    other = clone.parent / f"other-{branch}-{name}"
    _git(
        "clone",
        str(clone.parent / f"remote-{branch}.git"),
        str(other),
        cwd=clone.parent,
        check=True,
    )
    _git("config", "user.email", "t@t.com", cwd=other, check=True)
    _git("config", "user.name", "T", cwd=other, check=True)
    _git("config", "core.hooksPath", "/dev/null", cwd=other, check=True)
    _commit(other, name, content, f"feat: upstream {name}")
    _git("push", "origin", branch, cwd=other, check=True)


def test_dry_run_linear_fast_path_fetches_but_never_publishes(
    remote_and_clone, tmp_path
):
    clone = remote_and_clone("main")
    _commit(clone, "feature.txt", "work\n", "feat: local")
    before_remote = _bare_head(clone, "main")
    before_local = _local_state(clone)
    path, log = _git_spy(tmp_path)

    res = _run(SAFE_PUSH, clone, env_extra={"PATH": path}, args=("--dry-run",))
    assert res.returncode == 0, res.stderr
    assert "fast-forward" in res.stdout
    assert "tree " in res.stdout
    assert _bare_head(clone, "main") == before_remote
    assert _local_state(clone) == before_local
    called = _git_subcommands(log)
    assert {"fetch", "merge-tree"} <= called, called
    assert not called & FORBIDDEN_PUBLISH, called


def test_dry_run_diverged_disjoint_squash_path_is_read_only(remote_and_clone, tmp_path):
    """Remote and local changed different files: squash path computes a merge tree
    but must not commit, push, or materialize the upstream file locally."""
    clone = remote_and_clone("main")
    _commit(clone, "feature.txt", "work\n", "feat: local")
    _advance_remote(clone, "main", "upstream.txt", "up\n")
    before_remote = _bare_head(clone, "main")
    before_local = _local_state(clone)
    path, log = _git_spy(tmp_path)

    res = _run(SAFE_PUSH, clone, env_extra={"PATH": path}, args=("--dry-run",))
    assert res.returncode == 0, res.stderr
    assert _bare_head(clone, "main") == before_remote
    assert _local_state(clone) == before_local
    assert "squash" in res.stdout
    assert "tree " in res.stdout
    assert not (clone / "upstream.txt").exists(), "dry run materialized upstream path"
    called = _git_subcommands(log)
    assert {"fetch", "merge-tree"} <= called, called
    assert not called & FORBIDDEN_PUBLISH, called


def test_dry_run_conflict_exits_one_naming_path(remote_and_clone, tmp_path):
    clone = remote_and_clone("main")
    _commit(clone, "README.md", "local\n", "feat: local edit")
    _advance_remote(clone, "main", "README.md", "upstream\n")
    before_remote = _bare_head(clone, "main")
    before_local = _local_state(clone)
    path, log = _git_spy(tmp_path)

    res = _run(SAFE_PUSH, clone, env_extra={"PATH": path}, args=("--dry-run",))
    assert res.returncode == 1, res.stdout + res.stderr
    assert "README.md" in res.stderr, res.stderr
    assert _bare_head(clone, "main") == before_remote
    assert _local_state(clone) == before_local
    assert not _git_subcommands(log) & FORBIDDEN_PUBLISH


def test_dry_run_already_in_sync_exits_zero(remote_and_clone, tmp_path):
    clone = remote_and_clone("main")
    before_remote = _bare_head(clone, "main")
    before_local = _local_state(clone)
    path, log = _git_spy(tmp_path)

    res = _run(SAFE_PUSH, clone, env_extra={"PATH": path}, args=("--dry-run",))
    assert res.returncode == 0, res.stderr
    assert _bare_head(clone, "main") == before_remote
    assert _local_state(clone) == before_local
    assert not _git_subcommands(log) & FORBIDDEN_PUBLISH


@pytest.mark.parametrize("args", [("--dry-run", "--bogus"), ("--bogus", "--help")])
def test_invalid_argument_combinations_never_invoke_git(tmp_path, args):
    path, log = _rejecting_path(tmp_path)
    res = _run(SAFE_PUSH, tmp_path, env_extra={"PATH": path}, args=args)
    assert res.returncode == 2
    assert "Usage" in res.stderr
    assert not log.exists()


def test_dry_run_preserves_staged_unstaged_and_untracked_content(remote_and_clone):
    clone = remote_and_clone("main")
    _commit(clone, "feature.txt", "committed\n", "feat: local")
    (clone / "feature.txt").write_text("staged\n")
    _git("add", "feature.txt", cwd=clone, check=True)
    (clone / "feature.txt").write_text("unstaged\n")
    (clone / "untracked.txt").write_text("private\n")
    _advance_remote(clone, "main", "upstream.txt", "up\n")
    index = clone / ".git" / "index"
    before_index = index.read_bytes()
    before_local = _local_state(clone)
    before_remote = _bare_head(clone, "main")

    res = _run(SAFE_PUSH, clone, args=("--dry-run",))
    assert res.returncode == 0, res.stderr
    assert index.read_bytes() == before_index
    assert _local_state(clone) == before_local
    assert (clone / "feature.txt").read_text() == "unstaged\n"
    assert (clone / "untracked.txt").read_text() == "private\n"
    assert not (clone / "upstream.txt").exists()
    assert _bare_head(clone, "main") == before_remote


def test_dry_run_does_not_reset_trunk_when_only_merge_commits_remain(
    remote_and_clone, tmp_path
):
    clone = remote_and_clone("main")
    _git("checkout", "-b", "side", cwd=clone, check=True)
    _commit(clone, "side.txt", "side\n", "feat: side")
    _git("push", "origin", "side:main", cwd=clone, check=True)
    _git("checkout", "main", cwd=clone, check=True)
    _git("merge", "--no-ff", "side", "-m", "merge side", cwd=clone, check=True)
    before_local = _local_state(clone)
    before_remote = _bare_head(clone, "main")
    path, log = _git_spy(tmp_path)

    res = _run(SAFE_PUSH, clone, env_extra={"PATH": path}, args=("--dry-run",))
    assert res.returncode == 0, res.stderr
    assert "would reset" in res.stdout
    assert _local_state(clone) == before_local
    assert _bare_head(clone, "main") == before_remote
    assert not _git_subcommands(log) & FORBIDDEN_PUBLISH


@pytest.mark.parametrize("arg", ["--bogus", "--dry-run", "main"])
def test_pull_rejects_unknown_arguments_before_git_or_lock(tmp_path, arg):
    path, log = _rejecting_path(tmp_path)
    res = _run(SAFE_PULL, tmp_path, env_extra={"PATH": path}, args=(arg,))
    assert res.returncode == 2
    assert "unknown argument" in res.stderr
    assert not log.exists()
