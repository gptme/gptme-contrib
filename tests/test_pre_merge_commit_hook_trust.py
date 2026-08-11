"""The global pre-merge-commit hook must never exec repo-local code.

core.hooksPath is set globally, so this hook fires in every repository on the
machine. The original design delegated to a repo-local guard script after
verifying the guard's blob against refs/remotes/origin/master. But every trust
anchor inside .git/ is attacker-controlled in a crafted directory: an attacker
sets remote.origin.url to a trusted org and points refs/remotes/origin/master at
their commit — trusted_blob == work_blob, exec "$guard" runs the payload.

Fix: the hook inlines the brain-repo check without any in-repo delegation.
Worst case for a spoofed origin URL is a blocked merge (false positive), not RCE.
(ErikBjare/bob#1122 — four P0s on this file, all found by our own AI reviewer.)
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

HOOK = (
    Path(__file__).resolve().parents[1]
    / "dotfiles"
    / ".config"
    / "git"
    / "hooks"
    / "pre-merge-commit"
)


def _make_repo(tmp_path: Path, origin: str | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    if origin is not None:
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin", origin], check=True
        )
    (repo / "README").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--no-verify", "-qm", "init"],
        check=True,
        capture_output=True,
    )
    return repo


def _run_hook(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(HOOK)], cwd=repo, capture_output=True, text=True, timeout=30
    )


# ---------------------------------------------------------------------------
# Non-bob repos are always a no-op.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        "git@github.com:gptme/gptme-contrib.git",
        "https://github.com/evil/pwn.git",
        "git@github.com:ErikBjare/not-bob.git",
        "https://github.com/ErikBjare/bob-clone.git",
        None,  # no origin at all
    ],
)
def test_non_bob_repo_is_noop(tmp_path: Path, origin: str | None) -> None:
    """Hook exits 0 for any repo that isn't ErikBjare/bob."""
    repo = _make_repo(tmp_path, origin)
    proc = _run_hook(repo)
    assert (
        proc.returncode == 0
    ), f"unexpected exit {proc.returncode} for origin={origin!r}\n{proc.stderr}"


# ---------------------------------------------------------------------------
# Within the brain repo, the hook only fires on master with an active merge.
# ---------------------------------------------------------------------------


def test_non_bob_repo_with_active_merge_is_noop(tmp_path: Path) -> None:
    """Hook exits 0 for repos whose names merely contain 'bob' as a substring."""
    for i, origin in enumerate(
        (
            "https://github.com/ErikBjare/bob-clone.git",
            "https://github.com/ErikBjare/bob-backup.git",
            "git@github.com:ErikBjare/bob2.git",
        )
    ):
        sub = tmp_path / str(i)
        sub.mkdir()
        repo = _make_repo(sub, origin)
        # Simulate an active merge — the old glob *ErikBjare/bob* wrongly blocked this.
        (repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n")
        proc = _run_hook(repo)
        assert (
            proc.returncode == 0
        ), f"false positive: hook blocked merge in {origin!r}\n{proc.stderr}"


def test_bob_repo_not_on_master_is_noop(tmp_path: Path) -> None:
    """Hook exits 0 when not on master, even in the brain repo."""
    repo = _make_repo(tmp_path, "git@github.com:ErikBjare/bob.git")
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", "feature"],
        check=True,
        capture_output=True,
    )
    proc = _run_hook(repo)
    assert proc.returncode == 0


def test_bob_repo_on_master_no_merge_head_is_noop(tmp_path: Path) -> None:
    """Hook exits 0 on master when no merge is in progress."""
    repo = _make_repo(tmp_path, "https://github.com/ErikBjare/bob.git")
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    proc = _run_hook(repo)
    assert proc.returncode == 0


@pytest.mark.parametrize(
    "origin",
    [
        "git@github.com:ErikBjare/bob.git",
        "https://github.com/ErikBjare/bob.git",
        "https://github.com/erikbjare/bob.git",  # lowercase — GitHub is case-insensitive
        "https://github.com/ErikBjare/bob",  # no .git suffix
        "https://GITHUB.COM/ERIKBJARE/BOB.GIT",  # all-caps
    ],
)
def test_bob_repo_merge_on_master_is_blocked(tmp_path: Path, origin: str) -> None:
    """Hook exits 1 when a merge commit is about to land on master."""
    repo = _make_repo(tmp_path, origin)
    (repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n")
    proc = _run_hook(repo)
    assert (
        proc.returncode == 1
    ), f"merge was not blocked for origin={origin!r}\n{proc.stderr}"
    assert "Refusing" in proc.stderr or "🚫" in proc.stderr


# ---------------------------------------------------------------------------
# Core invariant: no repo-local code is ever executed.
#
# The original design exec'd $guard in trusted repos. An attacker supplies a
# crafted .git/ directory where refs/remotes/origin/master points at their
# commit — trusted_blob == work_blob, the dispatcher execs the payload. This
# test confirms the new hook never touches repo-local paths at all.
# ---------------------------------------------------------------------------


def test_no_repo_local_code_is_ever_executed(tmp_path: Path) -> None:
    """A repo-local script at the old guard path must never be executed."""
    repo = _make_repo(tmp_path, "https://github.com/ErikBjare/bob.git")
    sentinel = tmp_path / "PWNED"
    guard = repo / "scripts" / "precommit" / "prevent-master-merge-commits.sh"
    guard.parent.mkdir(parents=True)
    guard.write_text(f"#!/usr/bin/env bash\ntouch {sentinel}\nexit 0\n")
    guard.chmod(0o755)
    # Trigger the merge-on-master condition
    (repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n")
    proc = _run_hook(repo)
    # Hook must block the merge AND never exec the guard
    assert proc.returncode == 1, "merge was not blocked"
    assert not sentinel.exists(), "hook exec'd a repo-local script (RCE regression)"


def test_crafted_git_dir_with_planted_refs_cannot_cause_rce(tmp_path: Path) -> None:
    """Attacker-controlled .git/ refs cannot make the hook exec their code.

    Old design: attacker sets refs/remotes/origin/master to their commit
    (which has the guard) → trusted_blob == work_blob → exec → RCE.
    New design: no exec ever happens, regardless of what refs contain.
    """
    repo = _make_repo(tmp_path, "https://github.com/ErikBjare/bob.git")
    sentinel = tmp_path / "PWNED"
    guard = repo / "scripts" / "precommit" / "prevent-master-merge-commits.sh"
    guard.parent.mkdir(parents=True)
    guard.write_text(f"#!/usr/bin/env bash\ntouch {sentinel}\nexit 0\n")
    guard.chmod(0o755)
    # Attacker commits the guard and plants the ref
    subprocess.run(
        ["git", "-C", str(repo), "add", str(guard)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--no-verify", "-qm", "plant guard"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", "HEAD"],
        check=True,
    )
    (repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n")
    _run_hook(repo)
    assert not sentinel.exists(), "crafted .git/ refs caused RCE"
