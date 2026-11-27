"""Brain-history ancestry guard: refuse private brain SHAs on non-bob remotes.

ErikBjare/bob#1617: a brain checkout that also carries TimeToBuildBob/gptme as
``bob``/``fork`` leaked private history when a session pushed a brain branch to
the public fork. The guard is history-aware so gptme/contrib worktree pushes
(unrelated ancestry) still reach those remotes.

Tests invoke the hook directly. ``git push --dry-run`` skips pre-push hooks.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "dotfiles" / ".config" / "git" / "hooks" / "pre-push"

ZERO = "0" * 40
BOB_SSH = "git@github.com:ErikBjare/bob.git"
BOB_HTTPS = "https://github.com/ErikBjare/bob.git"
FORK_SSH = "git@github.com:TimeToBuildBob/gptme.git"
GPTME_SSH = "git@github.com:gptme/gptme.git"
CONTRIB_SSH = "git@github.com:gptme/gptme-contrib.git"
DEFAULT_ROOT = "de85643adbc014d0e47fbd92a2367f8df498526c"
BLOCK_MARK = "descends from brain root"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


def _init(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    return repo


def _commit(repo: Path, name: str, body: str = "x\n") -> str:
    (repo / name).write_text(body)
    _git(repo, "add", name)
    _git(repo, "commit", "--no-verify", "-qm", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


def _run_hook(
    repo: Path,
    stdin: str,
    remote: str,
    url: str,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    full_env = {
        **os.environ,
        "COORDINATION_DB": str(repo.parent / "coord.db"),
        "AGENT_WORKSPACE": str(repo.parent),
        "ALLOW_MASS_DELETE": "1",
        "ALLOW_FORCE_RESET_PUSH": "1",
        **(env or {}),
    }
    return subprocess.run(
        [str(HOOK), remote, url],
        cwd=repo,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        env=full_env,
    )


def _topic_stdin(sha: str) -> str:
    return f"refs/heads/topic {sha} refs/heads/topic {ZERO}\n"


def test_default_brain_root_is_bob_initial_commit() -> None:
    text = HOOK.read_text()
    assert DEFAULT_ROOT in text
    assert "github.com/erikbjare/bob" in text


@pytest.mark.parametrize(
    "remote,url",
    [
        ("bob", FORK_SSH),
        ("fork", FORK_SSH),
        ("gptme", GPTME_SSH),
        ("contrib", CONTRIB_SSH),
    ],
)
def test_brain_ref_refused_on_non_bob_remotes(
    tmp_path: Path, remote: str, url: str
) -> None:
    repo = _init(tmp_path)
    root = _commit(repo, "root.txt")
    child = _commit(repo, "child.txt")
    proc = _run_hook(
        repo,
        _topic_stdin(child),
        remote,
        url,
        env={"BRAIN_HISTORY_ROOTS": root},
    )
    assert proc.returncode == 1, proc.stderr
    assert BLOCK_MARK in proc.stderr, proc.stderr
    assert root in proc.stderr
    assert "ErikBjare/bob" in proc.stderr


@pytest.mark.parametrize(
    "url", [BOB_SSH, BOB_HTTPS, "git@github.com:erikbjare/bob.git"]
)
def test_brain_ref_allowed_to_erikbjare_bob(tmp_path: Path, url: str) -> None:
    repo = _init(tmp_path)
    root = _commit(repo, "root.txt")
    child = _commit(repo, "child.txt")
    proc = _run_hook(
        repo,
        _topic_stdin(child),
        "origin",
        url,
        env={"BRAIN_HISTORY_ROOTS": root},
    )
    assert proc.returncode == 0, proc.stderr
    assert BLOCK_MARK not in proc.stderr


def test_unrelated_ref_allowed_to_fork(tmp_path: Path) -> None:
    """A gptme worktree SHA does not descend from the brain root."""
    repo = _init(tmp_path)
    brain = _commit(repo, "brain.txt")
    _git(repo, "checkout", "-q", "--orphan", "gptme")
    _git(repo, "rm", "-rf", "--quiet", ".")
    other = _commit(repo, "gptme.txt", "unrelated\n")
    proc = _run_hook(
        repo,
        _topic_stdin(other),
        "fork",
        FORK_SSH,
        env={"BRAIN_HISTORY_ROOTS": brain},
    )
    assert proc.returncode == 0, proc.stderr
    assert BLOCK_MARK not in proc.stderr


def test_missing_default_root_is_a_skip_not_a_block(tmp_path: Path) -> None:
    """Other agents' object stores do not contain Bob's brain root."""
    repo = _init(tmp_path)
    head = _commit(repo, "only.txt")
    proc = _run_hook(repo, _topic_stdin(head), "fork", FORK_SSH)
    assert proc.returncode == 0, proc.stderr
    assert BLOCK_MARK not in proc.stderr


def test_branch_deletion_is_not_checked(tmp_path: Path) -> None:
    repo = _init(tmp_path)
    root = _commit(repo, "root.txt")
    stdin = f"refs/heads/topic {ZERO} refs/heads/topic {root}\n"
    proc = _run_hook(repo, stdin, "fork", FORK_SSH, env={"BRAIN_HISTORY_ROOTS": root})
    assert proc.returncode == 0, proc.stderr
    assert BLOCK_MARK not in proc.stderr


def test_allow_override_is_logged_and_skips(tmp_path: Path) -> None:
    repo = _init(tmp_path)
    root = _commit(repo, "root.txt")
    child = _commit(repo, "child.txt")
    proc = _run_hook(
        repo,
        _topic_stdin(child),
        "fork",
        FORK_SSH,
        env={"BRAIN_HISTORY_ROOTS": root, "ALLOW_BRAIN_HISTORY_PUSH": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "ALLOW_BRAIN_HISTORY_PUSH is set" in proc.stderr
    assert BLOCK_MARK not in proc.stderr


def test_unresolvable_local_sha_fails_closed(tmp_path: Path) -> None:
    repo = _init(tmp_path)
    root = _commit(repo, "root.txt")
    missing = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    proc = _run_hook(
        repo,
        _topic_stdin(missing),
        "fork",
        FORK_SSH,
        env={"BRAIN_HISTORY_ROOTS": root},
    )
    assert proc.returncode == 1, proc.stderr
    assert "cannot resolve" in proc.stderr


def test_roots_override_is_logged(tmp_path: Path) -> None:
    repo = _init(tmp_path)
    root = _commit(repo, "root.txt")
    proc = _run_hook(
        repo,
        _topic_stdin(root),
        "origin",
        BOB_SSH,
        env={"BRAIN_HISTORY_ROOTS": root},
    )
    assert proc.returncode == 0, proc.stderr
    assert "BRAIN_HISTORY_ROOTS override in effect" in proc.stderr
