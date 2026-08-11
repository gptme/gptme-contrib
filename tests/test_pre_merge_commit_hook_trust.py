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

For clean merges, Git invokes pre-merge-commit before MERGE_HEAD exists. Hook
invocation is the merge signal; checking MERGE_HEAD here silently bypasses the
guard. MERGE_HEAD is only reliable for the pre-commit conflicted-merge fallback.

The pre-commit hook carries a companion guard (Part 0.7) for the conflicted-merge
path: git merge stops on conflicts, the user resolves them and runs `git commit`
(or `git merge --continue`), which invokes pre-commit but NOT pre-merge-commit.
MERGE_HEAD is still present at that point, so the pre-commit hook also blocks.
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

PRE_COMMIT_HOOK = (
    Path(__file__).resolve().parents[1]
    / "dotfiles"
    / ".config"
    / "git"
    / "hooks"
    / "pre-commit"
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


def _install_hooks(repo: Path) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.hooksPath", str(HOOK.parent)],
        check=True,
    )


def _commit_file(repo: Path, relative_path: str, content: str, message: str) -> None:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", relative_path], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--no-verify", "-qm", message],
        check=True,
        capture_output=True,
    )


def _make_clean_merge_fixture(tmp_path: Path) -> Path:
    repo = _make_repo(tmp_path, "git@github.com:ErikBjare/bob.git")
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", "topic"],
        check=True,
        capture_output=True,
    )
    _commit_file(repo, "topic.txt", "topic\n", "topic")
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "master"],
        check=True,
        capture_output=True,
    )
    _commit_file(repo, "master.txt", "master\n", "master")
    _install_hooks(repo)
    return repo


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
        # P1: non-GitHub host with matching path must NOT trigger the guard
        "https://gitlab.com/ErikBjare/bob.git",
        "git@gitlab.com:ErikBjare/bob.git",
        None,  # no origin at all
    ],
)
def test_non_bob_repo_is_noop(tmp_path: Path, origin: str | None) -> None:
    """Hook exits 0 for any repo that isn't ErikBjare/bob on GitHub."""
    repo = _make_repo(tmp_path, origin)
    proc = _run_hook(repo)
    assert (
        proc.returncode == 0
    ), f"unexpected exit {proc.returncode} for origin={origin!r}\n{proc.stderr}"


# ---------------------------------------------------------------------------
# Within the brain repo, the hook only fires when invoked on master.
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


def test_bob_repo_on_master_blocks_when_git_invokes_pre_merge_commit(
    tmp_path: Path,
) -> None:
    """MERGE_HEAD is not available here; hook invocation is the merge signal."""
    repo = _make_repo(tmp_path, "https://github.com/ErikBjare/bob.git")
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    proc = _run_hook(repo)
    assert proc.returncode == 1
    assert "Refusing" in proc.stderr or "🚫" in proc.stderr


@pytest.mark.parametrize(
    "origin",
    [
        "git@github.com:ErikBjare/bob.git",
        "https://github.com/ErikBjare/bob.git",
        "https://github.com/erikbjare/bob.git",  # lowercase — GitHub is case-insensitive
        "https://github.com/ErikBjare/bob",  # no .git suffix
        "https://GITHUB.COM/ERIKBJARE/BOB.GIT",  # all-caps
        "https://github.com/ErikBjare/bob/",  # trailing slash after repo
        "https://github.com/ErikBjare/bob.git/",  # trailing slash after .git
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


def test_real_git_merge_on_master_in_brain_repo_is_blocked(tmp_path: Path) -> None:
    """A real clean git merge on master invokes pre-merge-commit and fails."""
    repo = _make_clean_merge_fixture(tmp_path)
    before = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"])

    proc = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-ff", "topic", "-m", "merge topic"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    after = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"])
    assert proc.returncode == 1, proc.stderr
    assert after == before, "blocked merge still advanced HEAD"
    assert "Refusing" in proc.stderr or "🚫" in proc.stderr, proc.stderr


def test_real_git_fast_forward_on_master_in_brain_repo_is_allowed(
    tmp_path: Path,
) -> None:
    """Fast-forward updates do not create merge commits, so the hook stays silent."""
    repo = _make_repo(tmp_path, "git@github.com:ErikBjare/bob.git")
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", "topic"],
        check=True,
        capture_output=True,
    )
    _commit_file(repo, "topic.txt", "topic\n", "topic")
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "master"],
        check=True,
        capture_output=True,
    )
    _install_hooks(repo)

    proc = subprocess.run(
        ["git", "-C", str(repo), "merge", "topic"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Refusing" not in proc.stderr
    assert "🚫" not in proc.stderr


def test_real_git_merge_on_non_master_in_brain_repo_is_allowed(
    tmp_path: Path,
) -> None:
    """The global hook does not block merge commits away from master."""
    repo = _make_repo(tmp_path, "git@github.com:ErikBjare/bob.git")
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", "topic"],
        check=True,
        capture_output=True,
    )
    _commit_file(repo, "topic.txt", "topic\n", "topic")
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "master"],
        check=True,
        capture_output=True,
    )
    _commit_file(repo, "master.txt", "master\n", "master")
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", "integration"],
        check=True,
        capture_output=True,
    )
    _install_hooks(repo)

    proc = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-ff", "topic", "-m", "merge topic"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Refusing" not in proc.stderr
    assert "🚫" not in proc.stderr


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


# ---------------------------------------------------------------------------
# pre-commit hook: conflicted-merge path (P1 regression)
#
# git merge stops on conflicts; the user resolves them and runs `git commit`
# (or `git merge --continue`). That invokes pre-commit but NOT pre-merge-commit.
# MERGE_HEAD is still present at that point. The pre-commit hook (Part 0.7)
# must intercept this path in the brain repo — otherwise a conflict-resolved
# pull on master still mints the merge commit that origin's ruleset rejects.
# ---------------------------------------------------------------------------


def _run_pre_commit_hook(repo: Path) -> subprocess.CompletedProcess:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(repo.parent),
        # Suppress the identity and allowed-repos guards so the test is scoped
        # to the merge-commit check only.
        "ALLOW_GIT_IDENTITY": "1",
        "ALLOW_MASTER_COMMITS": "1",
    }
    return subprocess.run(
        [str(PRE_COMMIT_HOOK)],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


@pytest.mark.parametrize(
    "origin",
    [
        "git@github.com:ErikBjare/bob.git",
        "https://github.com/ErikBjare/bob.git",
        "https://github.com/erikbjare/bob.git",
        "https://GITHUB.COM/ERIKBJARE/BOB.GIT",
        "https://github.com/ErikBjare/bob/",  # trailing slash after repo
        "https://github.com/ErikBjare/bob.git/",  # trailing slash after .git
    ],
)
def test_pre_commit_blocks_conflicted_merge_on_master_in_brain_repo(
    tmp_path: Path, origin: str
) -> None:
    """pre-commit must block a conflict-resolved merge on master (pre-merge-commit gap)."""
    repo = _make_repo(tmp_path, origin)
    (repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n")
    proc = _run_pre_commit_hook(repo)
    assert (
        proc.returncode == 1
    ), f"pre-commit did not block conflicted merge for origin={origin!r}\n{proc.stderr}"
    assert "Refusing" in proc.stderr or "🚫" in proc.stderr, proc.stderr


def test_pre_commit_does_not_block_non_merge_commit_in_brain_repo(
    tmp_path: Path,
) -> None:
    """pre-commit must not block normal commits (no MERGE_HEAD) on master."""
    repo = _make_repo(tmp_path, "git@github.com:ErikBjare/bob.git")
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    proc = _run_pre_commit_hook(repo)
    # The hook may exit non-zero for other reasons (identity check, etc.) but
    # must NOT emit the merge-commit refusal — that guard must stay silent.
    assert "Refusing" not in proc.stderr
    assert "🚫" not in proc.stderr


@pytest.mark.parametrize(
    "origin",
    [
        "git@github.com:gptme/gptme-contrib.git",
        # P1: same path on a non-GitHub host must NOT trigger the guard
        "https://gitlab.com/ErikBjare/bob.git",
        "git@gitlab.com:ErikBjare/bob.git",
    ],
)
def test_pre_commit_does_not_block_conflicted_merge_in_non_brain_repo(
    tmp_path: Path, origin: str
) -> None:
    """pre-commit merge guard is brain-repo-only; other repos are unaffected."""
    repo = _make_repo(tmp_path, origin)
    (repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n")
    proc = _run_pre_commit_hook(repo)
    assert "Refusing" not in proc.stderr, f"false positive for origin={origin!r}"
    assert "🚫" not in proc.stderr, f"false positive for origin={origin!r}"
