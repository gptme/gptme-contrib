"""The global pre-merge-commit dispatcher must not run untrusted repo content.

``core.hooksPath`` is set globally, so this hook fires in *every* repository,
including a fresh clone of somebody else's. The dispatcher delegates to
``$root/scripts/precommit/prevent-master-merge-commits.sh`` — a path controlled
by whatever repo you happen to be standing in.

The original version exec'd that script whenever it existed and was executable.
An adversary commits an executable file at exactly that path; you clone the
repo, run an ordinary ``git merge``, and their script runs with your
privileges. Git's usual hook model is opt-in *because* ``.git/hooks`` is never
fetched from a remote — ``core.hooksPath`` opts every repo in at once, so the
trust check has to be reintroduced in the dispatcher itself.

Found as a P0 by our own self-hosted AI reviewer on gptme/gptme-contrib#1380
(ErikBjare/bob#1122). These tests pin the trust boundary.
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


def _git(repo: Path, *args: str) -> None:
    # `--no-verify` on the fixture commits: Bob's global core.hooksPath points
    # at the real hook set, which this temp repo cannot satisfy. Scoped to the
    # fixture rather than overriding hooksPath, which has leaked before.
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(
    tmp_path: Path, origin: str | None, *, commit_guard: bool = True
) -> tuple[Path, Path]:
    """A git repo carrying a guard that touches a sentinel file when executed.

    ``commit_guard=False`` leaves the guard present in the worktree but absent
    from HEAD — the shape a merge produces when an incoming branch *introduces*
    the file. The hook must refuse that even in a trusted repo.
    """
    repo = tmp_path / "repo"
    (repo / "scripts" / "precommit").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    if origin is not None:
        _git(repo, "remote", "add", "origin", origin)

    # A commit must exist so HEAD resolves at all.
    (repo / "README").write_text("x\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "--no-verify", "-qm", "init")

    sentinel = tmp_path / "PWNED"
    guard = repo / "scripts" / "precommit" / "prevent-master-merge-commits.sh"
    guard.write_text(f"#!/usr/bin/env bash\ntouch {sentinel}\nexit 0\n")
    guard.chmod(0o755)
    if commit_guard:
        _git(repo, "add", str(guard))
        _git(repo, "commit", "--no-verify", "-qm", "add guard")
    return repo, sentinel


def _run_hook(repo: Path) -> None:
    subprocess.run([str(HOOK)], cwd=repo, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize(
    "origin",
    [
        "git@github.com:evil/pwn.git",
        "https://github.com/evil/pwn.git",
        # Must not match by mere substring — this is why the owner is anchored.
        "https://evil.com/gptme-fake/x.git",
        "https://github.com/gptme-evil/x.git",
        "https://github.com/notErikBjare/x.git",
        None,  # no origin at all
        # --- attacker HOST carrying an EXACT allowed owner -------------------
        # The first fix checked the owner and ignored the host, so all of these
        # executed the payload while the cases above correctly refused. The
        # original matrix only ever varied the owner, so nothing caught it.
        # An attacker just names their account after one of ours.
        "https://evil.com/gptme/malicious.git",
        "git@evil.com:gptme/malicious.git",
        "https://evil.com/ErikBjare/bob.git",
        "ssh://git@evil.com:3000/ErikBjare/bob.git",
        "https://github.com.evil.com/gptme/x.git",
    ],
)
def test_untrusted_repo_guard_is_not_executed(
    tmp_path: Path, origin: str | None
) -> None:
    repo, sentinel = _make_repo(tmp_path, origin)
    _run_hook(repo)
    assert (
        not sentinel.exists()
    ), f"executed repo-controlled script for origin={origin!r}"


@pytest.mark.parametrize(
    "origin",
    [
        "git@github.com:gptme/gptme-contrib.git",
        "https://github.com/ErikBjare/bob.git",
        "git@github.com:ActivityWatch/activitywatch.git",
        # Forgejo, with a port in the URL.
        "ssh://git@forgejo.hassel.bjareho.lt:3000/ErikBjare/bob.git",
    ],
)
def test_trusted_repo_guard_still_runs(tmp_path: Path, origin: str) -> None:
    """The guard must keep working where it is actually wanted."""
    repo, sentinel = _make_repo(tmp_path, origin)
    _run_hook(repo)
    assert sentinel.exists(), f"guard did not run in trusted repo origin={origin!r}"


def test_absent_guard_is_a_noop(tmp_path: Path) -> None:
    """Most repos ship no guard; the hook must exit cleanly and quietly."""
    repo, _ = _make_repo(tmp_path, "git@github.com:gptme/gptme-contrib.git")
    (repo / "scripts" / "precommit" / "prevent-master-merge-commits.sh").unlink()
    proc = subprocess.run(
        [str(HOOK)], cwd=repo, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0
    assert proc.stdout == ""


# ---------------------------------------------------------------------------
# A trusted ORIGIN does not make merged-in CONTENT trusted.
#
# pre-merge-commit fires after the merge has been applied to the worktree and
# before the commit object exists. So a branch from an untrusted fork can carry
# this exact file into a repo whose origin is on the allowlist, and the hook
# would execute the attacker's version. The origin check cannot see that — it
# only describes where the repo came from.
#
# Third P0 our own AI reviewer found on this file (ErikBjare/bob#1122); the
# previous two rounds of tests only ever varied the origin URL.
# ---------------------------------------------------------------------------

TRUSTED = "git@github.com:gptme/gptme-contrib.git"


def test_guard_introduced_by_the_merge_is_not_executed(tmp_path: Path) -> None:
    """The file is in the worktree but not at HEAD — i.e. the merge added it."""
    repo, sentinel = _make_repo(tmp_path, TRUSTED, commit_guard=False)
    _run_hook(repo)
    assert not sentinel.exists(), "executed a guard that the merge introduced"


def test_guard_modified_by_the_merge_is_not_executed(tmp_path: Path) -> None:
    """Committing a benign guard then having the merge edit it must not help."""
    repo, sentinel = _make_repo(tmp_path, TRUSTED, commit_guard=True)
    guard = repo / "scripts" / "precommit" / "prevent-master-merge-commits.sh"
    # The merge rewrites the committed guard with a hostile payload.
    guard.write_text(f"#!/usr/bin/env bash\ntouch {sentinel}\nexit 0\n# owned\n")
    guard.chmod(0o755)
    _run_hook(repo)
    assert not sentinel.exists(), "executed a guard the merge had modified"


def test_committed_unmodified_guard_still_runs(tmp_path: Path) -> None:
    """The legitimate case must keep working, or the hook is just disabled."""
    repo, sentinel = _make_repo(tmp_path, TRUSTED, commit_guard=True)
    _run_hook(repo)
    assert sentinel.exists(), "the repo's own committed guard did not run"
