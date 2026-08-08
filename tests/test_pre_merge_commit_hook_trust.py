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
    tmp_path: Path,
    origin: str | None,
    *,
    commit_guard: bool = True,
    trusted_ref: str | None = "refs/remotes/origin/master",
) -> tuple[Path, Path]:
    """A git repo carrying a guard that touches a sentinel file when executed.

    ``commit_guard=False`` leaves the guard present in the worktree but absent
    from the trusted ref — the shape a merge produces when an incoming branch
    *introduces* the file. The hook must refuse that even in a trusted repo.

    ``trusted_ref`` is pointed at whatever HEAD is once the guard has (or has
    not) been committed, standing in for what a ``git fetch origin`` would have
    left behind. ``None`` builds a repo with no remote-tracking refs at all.
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
    if trusted_ref is not None:
        _git(repo, "update-ref", trusted_ref, "HEAD")
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


# ---------------------------------------------------------------------------
# ...and HEAD is not the trust anchor either.
#
# The previous round anchored to ``HEAD:$guard_rel``. But HEAD is wherever you
# are standing: ``gh pr checkout <hostile-pr>`` moves it onto the contributor's
# branch. The worktree then matches HEAD by construction, the origin is still on
# the allowlist, and the next ``git merge`` in that checkout execs the payload.
#
# The anchor has to be a ref only ``git fetch origin`` can move. Fourth P0 on
# this file (ErikBjare/bob#1122).
# ---------------------------------------------------------------------------


def test_guard_committed_on_a_checked_out_pr_branch_is_not_executed(
    tmp_path: Path,
) -> None:
    """The exact ``gh pr checkout`` shape: hostile guard at HEAD, clean origin.

    Under the HEAD anchor this passed every check — worktree == HEAD, origin
    allowlisted — and ran the payload.
    """
    # origin/master is planted at the guard-less init commit...
    repo, sentinel = _make_repo(
        tmp_path, TRUSTED, commit_guard=False, trusted_ref="refs/remotes/origin/master"
    )
    # ...then the contributor's branch commits the payload, so HEAD carries it
    # and the worktree matches HEAD exactly.
    guard = repo / "scripts" / "precommit" / "prevent-master-merge-commits.sh"
    _git(repo, "add", str(guard))
    _git(repo, "commit", "--no-verify", "-qm", "hostile PR branch adds a guard")
    _run_hook(repo)
    assert not sentinel.exists(), "executed a guard that only exists on a PR branch"


def test_guard_edited_on_a_checked_out_pr_branch_is_not_executed(
    tmp_path: Path,
) -> None:
    """Same shape, but the PR *edits* a guard that origin already ships."""
    repo, sentinel = _make_repo(tmp_path, TRUSTED, commit_guard=True)
    guard = repo / "scripts" / "precommit" / "prevent-master-merge-commits.sh"
    guard.write_text(f"#!/usr/bin/env bash\ntouch {sentinel}\nexit 0\n# owned\n")
    guard.chmod(0o755)
    _git(repo, "add", str(guard))
    _git(repo, "commit", "--no-verify", "-qm", "hostile PR branch edits the guard")
    _run_hook(repo)
    assert not sentinel.exists(), "executed a guard a PR branch had edited"


@pytest.mark.parametrize(
    "trusted_ref",
    [
        "refs/remotes/origin/master",
        "refs/remotes/origin/main",
        "refs/remotes/origin/HEAD",
    ],
)
def test_unambiguous_origin_default_branch_ref_anchors_trust(
    tmp_path: Path, trusted_ref: str
) -> None:
    """A symbolic HEAD or a sole conventional default ref can anchor."""
    repo, sentinel = _make_repo(tmp_path, TRUSTED, trusted_ref=trusted_ref)
    _run_hook(repo)
    assert sentinel.exists(), f"guard did not run with anchor {trusted_ref}"


def test_no_remote_tracking_ref_is_inert(tmp_path: Path) -> None:
    """Nothing to anchor against means no trust. Fail closed, quietly."""
    repo, sentinel = _make_repo(tmp_path, TRUSTED, trusted_ref=None)
    proc = subprocess.run(
        [str(HOOK)], cwd=repo, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0
    assert not sentinel.exists(), "ran the guard with no trusted ref to anchor to"


def test_a_non_default_origin_branch_does_not_anchor(tmp_path: Path) -> None:
    """A PR branch pushed to origin is still just a branch, not the anchor."""
    repo, sentinel = _make_repo(
        tmp_path, TRUSTED, trusted_ref="refs/remotes/origin/some-pr-branch"
    )
    _run_hook(repo)
    assert not sentinel.exists(), "anchored trust to a non-default origin branch"


# ---------------------------------------------------------------------------
# Case folding, and picking the right ref.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        "https://GITHUB.com/gptme/gptme-contrib.git",
        "https://github.com/GPTME/gptme-contrib.git",
        "https://GitHub.Com/ErikBjare/bob.git",
        "HTTPS://github.com/gptme/gptme-contrib.git",
        "git@GitHub.com:ActivityWatch/activitywatch.git",
    ],
)
def test_case_variants_of_a_trusted_origin_still_run(
    tmp_path: Path, origin: str
) -> None:
    """Byte-exact matching made the hook silently inert for real maintainers.

    Hostnames are case-insensitive by DNS, and GitHub/Forgejo account names are
    case-insensitive and unique case-insensitively, so folding case cannot admit
    a different principal.
    """
    repo, sentinel = _make_repo(tmp_path, origin)
    _run_hook(repo)
    assert sentinel.exists(), f"guard did not run for origin={origin!r}"


@pytest.mark.parametrize(
    "origin",
    [
        "https://EVIL.com/gptme/malicious.git",
        "https://GitHub.com.evil.com/gptme/x.git",
        "https://github.com/GPTME-EVIL/x.git",
        "git@EVIL.com:ErikBjare/bob.git",
    ],
)
def test_case_folding_does_not_admit_an_untrusted_origin(
    tmp_path: Path, origin: str
) -> None:
    """Folding case must not widen the allowlist to anything else."""
    repo, sentinel = _make_repo(tmp_path, origin)
    _run_hook(repo)
    assert not sentinel.exists(), f"executed for untrusted origin={origin!r}"


def _repo_with_stale_master_and_current_main(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo, sentinel = _make_repo(tmp_path, TRUSTED, commit_guard=True, trusted_ref=None)
    guard = repo / "scripts" / "precommit" / "prevent-master-merge-commits.sh"

    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    _git(repo, "rm", "-q", str(guard))
    _git(repo, "commit", "--no-verify", "-qm", "drop the guard on main")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    guard.parent.mkdir(parents=True, exist_ok=True)
    guard.write_text(f"#!/usr/bin/env bash\ntouch {sentinel}\nexit 0\n")
    guard.chmod(0o755)
    return repo, sentinel, guard


def test_stale_origin_master_does_not_anchor_a_main_default_repo(
    tmp_path: Path,
) -> None:
    """The anchor is origin's DEFAULT branch, not whichever ref happens to have it.

    Picking the first ref that resolves a *blob* let a stale origin/master
    resurrect a guard the maintainers had deleted from main — running policy the
    team removed, from a branch that is not their default.
    """
    repo, sentinel, _ = _repo_with_stale_master_and_current_main(tmp_path)
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    _run_hook(repo)
    assert not sentinel.exists(), "anchored to stale master instead of origin/HEAD"


def test_missing_origin_head_with_master_and_main_is_inert(tmp_path: Path) -> None:
    """Without origin/HEAD, two conventional refs are an ambiguous anchor."""
    repo, sentinel, _ = _repo_with_stale_master_and_current_main(tmp_path)

    _run_hook(repo)
    assert not sentinel.exists(), "guessed master while origin's default was unknown"
