"""The pre-push hook's origin-scoping and range-limit override must mean what they say.

Two defects our own AI reviewer found on gptme/gptme-contrib#1380
(ErikBjare/bob#1122), both of the same shape: a documented behaviour that the
code does not actually implement.

* The force-reset guard was scoped to origin by wrapping *only* the ``git fetch``
  in a remote-name check. The reflog comparison after it still read
  ``refs/remotes/origin/$branch`` unconditionally, so pushing to a mirror was
  still blocked by an origin force-reset.
* The skip message tells you to set ``MASS_DELETE_COMMIT_RANGE_LIMIT=0`` to force
  the full scan, but the comparison was ``count -gt limit`` — so 0 made *every*
  non-empty range take the skip branch and disabled the guard entirely.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "dotfiles" / ".config" / "git" / "hooks" / "pre-push"

ZERO = "0" * 40
FORCE_RESET_ERROR = "was force-reset"
SKIP_MESSAGE = "skipping per-commit mass-delete scan"


def _git(repo: Path, *args: str) -> str:
    # `--no-verify` on fixture commits: Bob's global core.hooksPath points at the
    # real hook set, which these temp repos cannot satisfy.
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
    # A dead origin URL: the hook's `git fetch origin` fails harmlessly (it is
    # `|| true`), which keeps the reflog we plant below under our control.
    _git(repo, "remote", "add", "origin", str(tmp_path / "nonexistent.git"))
    # A genuine mirror: a real remote at a DIFFERENT url.
    _git(repo, "remote", "add", "mirror", str(tmp_path / "mirror.git"))
    _git(repo, "remote", "add", "forgejo", str(tmp_path / "forgejo.git"))
    _git(repo, "remote", "add", "backup", str(tmp_path / "backup.git"))
    # An ALIAS of origin: a different name pointing at origin's own url.
    _git(repo, "remote", "add", "upstream", str(tmp_path / "nonexistent.git"))
    return repo


def _commit(repo: Path, name: str, body: str = "x\n") -> str:
    (repo / name).write_text(body)
    _git(repo, "add", name)
    _git(repo, "commit", "--no-verify", "-qm", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


def _run_hook(
    repo: Path, stdin: str, *argv: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        [str(HOOK), *argv],
        cwd=repo,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        env=full_env,
    )


# ---------------------------------------------------------------------------
# Force-reset guard: scoped to origin, in full.
# ---------------------------------------------------------------------------


def _repo_with_force_reset_reflog(tmp_path: Path) -> tuple[Path, str]:
    """A repo whose origin/master reflog holds a non-fast-forward transition."""
    repo = _init(tmp_path)
    base = _commit(repo, "README")
    _git(repo, "checkout", "-q", "-b", "sideline", base)
    diverged = _commit(repo, "sideline.txt")
    _git(repo, "checkout", "-q", "-")

    # Two reflog entries where the older is NOT an ancestor of the newer —
    # exactly what a human force-resetting origin/master leaves behind.
    _git(repo, "update-ref", "refs/remotes/origin/master", diverged)
    _git(repo, "update-ref", "refs/remotes/origin/master", base)

    head = _commit(repo, "local.txt")
    return repo, head


def test_force_reset_guard_fires_when_pushing_to_origin(tmp_path: Path) -> None:
    """The guard itself must still work — otherwise the fix just disables it."""
    repo, head = _repo_with_force_reset_reflog(tmp_path)
    proc = _run_hook(
        repo,
        f"refs/heads/master {head} refs/heads/master {head}\n",
        "origin",
        "git@github.com:o/r.git",
    )
    assert FORCE_RESET_ERROR in proc.stderr, proc.stderr
    assert proc.returncode == 1


@pytest.mark.parametrize("remote", ["mirror", "forgejo", "backup"])
def test_force_reset_guard_does_not_block_a_non_origin_push(
    tmp_path: Path, remote: str
) -> None:
    """The regression: a stale origin reflog must not block a mirror push.

    Skipping only the fetch left the reflog comparison in place, so this still
    exited 1 with an error about a remote the user was not pushing to.
    """
    repo, head = _repo_with_force_reset_reflog(tmp_path)
    proc = _run_hook(
        repo,
        f"refs/heads/master {head} refs/heads/master {head}\n",
        remote,
        f"git@example.com:o/{remote}.git",
    )
    assert FORCE_RESET_ERROR not in proc.stderr, proc.stderr


def test_force_reset_guard_still_applies_when_invoked_outside_git(
    tmp_path: Path,
) -> None:
    """No remote name (direct invocation) keeps the old, protective behaviour."""
    repo, head = _repo_with_force_reset_reflog(tmp_path)
    proc = _run_hook(repo, f"refs/heads/master {head} refs/heads/master {head}\n")
    assert FORCE_RESET_ERROR in proc.stderr, proc.stderr
    assert proc.returncode == 1


# ---------------------------------------------------------------------------
# MASS_DELETE_COMMIT_RANGE_LIMIT: 0 means "no limit", as documented.
# ---------------------------------------------------------------------------


def _repo_with_mass_deletion(tmp_path: Path) -> tuple[Path, str, str]:
    """A repo whose tip commit deletes more files than MASS_DELETE_THRESHOLD=1."""
    repo = _init(tmp_path)
    for name in ("a.txt", "b.txt", "c.txt"):
        (repo / name).write_text("x\n")
    _git(repo, "add", "a.txt", "b.txt", "c.txt")
    _git(repo, "commit", "--no-verify", "-qm", "seed")
    base = _git(repo, "rev-parse", "HEAD")

    _git(repo, "rm", "-q", "a.txt", "b.txt", "c.txt")
    _git(repo, "commit", "--no-verify", "-qm", "delete everything")
    head = _git(repo, "rev-parse", "HEAD")
    return repo, base, head


def test_range_limit_zero_forces_the_full_scan(tmp_path: Path) -> None:
    """The regression: 0 is the documented "force the scan" value.

    With `count -gt 0`, every non-empty range satisfied the skip condition, so
    the override silently disabled the guard it claimed to force on.
    """
    repo, base, head = _repo_with_mass_deletion(tmp_path)
    proc = _run_hook(
        repo,
        f"refs/heads/topic {head} refs/heads/topic {base}\n",
        "origin",
        "git@github.com:o/r.git",
        env={"MASS_DELETE_COMMIT_RANGE_LIMIT": "0", "MASS_DELETE_THRESHOLD": "1"},
    )
    assert SKIP_MESSAGE not in proc.stderr, proc.stderr
    # The scan ran, and the mass deletion it exists to catch was caught.
    assert proc.returncode == 1, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"


def test_range_limit_above_the_range_still_scans(tmp_path: Path) -> None:
    repo, base, head = _repo_with_mass_deletion(tmp_path)
    proc = _run_hook(
        repo,
        f"refs/heads/topic {head} refs/heads/topic {base}\n",
        "origin",
        "git@github.com:o/r.git",
        env={"MASS_DELETE_COMMIT_RANGE_LIMIT": "500", "MASS_DELETE_THRESHOLD": "1"},
    )
    assert SKIP_MESSAGE not in proc.stderr, proc.stderr
    assert proc.returncode == 1


def test_commits_already_on_another_remote_are_not_rescanned(tmp_path: Path) -> None:
    """A fork rebase must not re-check upstream's own landed mass deletion.

    The fork's branch sits at ``base``; upstream master already contains the
    deletion commit; the rebased branch adds one commit on top. Only that new
    commit is ours to check.
    """
    repo, base, head = _repo_with_mass_deletion(tmp_path)
    _git(repo, "update-ref", "refs/remotes/upstream/master", head)
    tip = _commit(repo, "new.txt")
    proc = _run_hook(
        repo,
        f"refs/heads/topic {tip} refs/heads/topic {base}\n",
        "fork",
        "git@github.com:me/r.git",
        env={"MASS_DELETE_COMMIT_RANGE_LIMIT": "0", "MASS_DELETE_THRESHOLD": "1"},
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"


@pytest.mark.parametrize(
    "new_branch,remote", [(False, "upstream"), (True, "upstream"), (True, "mirror")]
)
def test_pruned_exclusion_ref_does_not_disable_mass_delete_scan(
    tmp_path: Path, new_branch: bool, remote: str
) -> None:
    """Prune after enumeration, before rev-list, without a timing-dependent fetch."""
    repo, base, head = _repo_with_mass_deletion(tmp_path)
    ref = f"refs/remotes/{remote}/main"
    _git(repo, "update-ref", ref, base)
    real_git = shutil.which("git")
    assert real_git is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "git"
    wrapper.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = for-each-ref ]; then\n'
        '    output=$("$REAL_GIT" "$@") || exit $?\n'
        '    case "$*" in\n'
        '        *"$PRUNE_PREFIX"*) "$REAL_GIT" update-ref -d "$PRUNE_REF" ;;\n'
        "    esac\n"
        '    printf "%s\\n" "$output"\n'
        "else\n"
        '    exec "$REAL_GIT" "$@"\n'
        "fi\n"
    )
    wrapper.chmod(0o755)
    proc = _run_hook(
        repo,
        f"refs/heads/topic {head} refs/heads/topic {ZERO if new_branch else base}\n",
        "mirror",
        _git(repo, "remote", "get-url", "mirror"),
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "REAL_GIT": real_git,
            "PRUNE_REF": ref,
            "PRUNE_PREFIX": "refs/remotes/mirror/"
            if remote == "mirror"
            else "refs/remotes/",
            "MASS_DELETE_COMMIT_RANGE_LIMIT": "0",
            "MASS_DELETE_THRESHOLD": "1",
        },
    )
    assert not _git(repo, "for-each-ref", "--format=%(refname)", ref)
    assert proc.returncode == 1, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "PUSH BLOCKED: commit" in proc.stderr, proc.stderr


def test_stale_destination_tracking_ref_does_not_bypass_guard(tmp_path: Path) -> None:
    """A stale origin tracking ref must not let a destructive commit skip the scan.

    Scenario: someone force-reset ``origin/topic`` to ``base`` (deletion's parent).
    Our local ``refs/remotes/origin/topic`` still points to ``head`` (the deletion
    commit). Git passes ``remote_sha=base``. The guard must scan ``head``; only the
    destination remote's stale ref caused the problem, and we must not exclude it.
    """
    repo, base, head = _repo_with_mass_deletion(tmp_path)
    # Stale: our local tracking ref still points to the deletion commit.
    _git(repo, "update-ref", "refs/remotes/origin/topic", head)
    proc = _run_hook(
        repo,
        f"refs/heads/topic {head} refs/heads/topic {base}\n",
        "origin",
        "git@github.com:me/r.git",
        env={"MASS_DELETE_COMMIT_RANGE_LIMIT": "0", "MASS_DELETE_THRESHOLD": "1"},
    )
    assert proc.returncode == 1, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"


def test_stale_dest_tracking_ref_does_not_bypass_guard_on_new_branch(
    tmp_path: Path,
) -> None:
    """Stale destination tracking ref must not bypass the guard for a new-branch push.

    Scenario: ``origin/topic`` was deleted remotely but the clone still has
    ``refs/remotes/origin/topic`` pointing to the destructive commit. Git
    supplies ``remote_sha=ZERO`` (new branch). The old ``--remotes`` range
    would exclude ``origin/topic=D``, hiding D from the scan; the fix uses
    ``_other_remotes`` (which excludes only non-destination remotes).
    """
    repo, _base, head = _repo_with_mass_deletion(tmp_path)
    # Clone retains a stale tracking ref on the push destination.
    _git(repo, "update-ref", "refs/remotes/origin/topic", head)
    proc = _run_hook(
        repo,
        f"refs/heads/topic {head} refs/heads/topic {ZERO}\n",
        "origin",
        "git@github.com:me/r.git",
        env={"MASS_DELETE_COMMIT_RANGE_LIMIT": "0", "MASS_DELETE_THRESHOLD": "1"},
    )
    assert proc.returncode == 1, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"


def test_backup_remote_fetching_same_repo_is_not_excluded(tmp_path: Path) -> None:
    """A remote that fetches from the destination must not hide destructive commits.

    The exclusion list is built from push URLs, but tracking refs describe what
    was *fetched*. A 'backup' remote configured to fetch from the same repository
    as origin but push to a different forge can retain a stale tracking ref
    pointing to a destructive commit. That ref must not exclude the commit from
    the scan when we push to origin.
    """
    repo, base, head = _repo_with_mass_deletion(tmp_path)
    origin_url = _git(repo, "remote", "get-url", "origin").strip()
    # backup fetches from the same place as origin (same URL) but pushes elsewhere.
    _git(repo, "remote", "set-url", "backup", origin_url)
    _git(repo, "remote", "set-url", "--push", "backup", str(tmp_path / "backup.git"))
    # Stale tracking ref on backup pointing to the deletion commit.
    _git(repo, "update-ref", "refs/remotes/backup/topic", head)
    proc = _run_hook(
        repo,
        f"refs/heads/topic {head} refs/heads/topic {base}\n",
        "origin",
        origin_url,
        env={"MASS_DELETE_COMMIT_RANGE_LIMIT": "0", "MASS_DELETE_THRESHOLD": "1"},
    )
    assert proc.returncode == 1, (
        "guard should fire: backup's stale ref points to the deletion commit "
        f"but must not exclude it\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )


def test_range_limit_smaller_than_range_skips(tmp_path: Path) -> None:
    """The O(N) escape hatch must keep working for a genuinely huge range."""
    repo = _init(tmp_path)
    base = _commit(repo, "README")
    for i in range(3):
        _commit(repo, f"f{i}.txt")
    head = _git(repo, "rev-parse", "HEAD")
    proc = _run_hook(
        repo,
        f"refs/heads/topic {head} refs/heads/topic {base}\n",
        "origin",
        "git@github.com:o/r.git",
        env={"MASS_DELETE_COMMIT_RANGE_LIMIT": "1"},
    )
    assert SKIP_MESSAGE in proc.stderr, proc.stderr


def test_non_numeric_range_limit_falls_back_to_the_default(tmp_path: Path) -> None:
    """A typo must not error out or disable the guard."""
    repo, base, head = _repo_with_mass_deletion(tmp_path)
    proc = _run_hook(
        repo,
        f"refs/heads/topic {head} refs/heads/topic {base}\n",
        "origin",
        "git@github.com:o/r.git",
        env={"MASS_DELETE_COMMIT_RANGE_LIMIT": "lots", "MASS_DELETE_THRESHOLD": "1"},
    )
    assert "integer expression expected" not in proc.stderr, proc.stderr
    assert SKIP_MESSAGE not in proc.stderr, proc.stderr
    assert proc.returncode == 1


@pytest.mark.parametrize(
    ("origin_url", "alias_url"),
    [
        ("https://github.com/Org/Repo.git", "https://github.com/org/repo"),
        ("git@github.com:Org/Repo.git", "ssh://git@GITHUB.com/org/repo"),
        ("https://github.com/org/repo.git/", "git@github.com:org/repo.git"),
        # Explicit SSH port 22 (default) is still origin — the port must be stripped.
        ("git@github.com:org/repo.git", "ssh://git@github.com:22/org/repo.git"),
        # Explicit HTTPS port 443 (default) is still origin — well-known port stripped.
        ("https://github.com/org/repo.git", "https://github.com:443/org/repo.git"),
        # GitHub's alternative SSH host (port 443 for firewall traversal) is still origin.
        ("git@github.com:org/repo.git", "ssh://git@ssh.github.com:443/org/repo.git"),
    ],
)
def test_force_reset_guard_still_applies_to_a_remote_aliasing_origin(
    tmp_path: Path, origin_url: str, alias_url: str
) -> None:
    """Equivalent URL spellings still identify origin, not a mirror."""
    repo, head = _repo_with_force_reset_reflog(tmp_path)
    _git(repo, "remote", "set-url", "origin", origin_url)
    _git(repo, "remote", "set-url", "upstream", alias_url)
    proc = _run_hook(
        repo,
        f"refs/heads/master {head} refs/heads/master {head}\n",
        "upstream",
        alias_url,
    )
    assert FORCE_RESET_ERROR in proc.stderr, proc.stderr
    assert proc.returncode == 1


def test_force_reset_guard_does_not_block_push_to_non_standard_port(
    tmp_path: Path,
) -> None:
    """A non-standard port produces a distinct canonical identity — not origin.

    origin is https://github.com/org/repo.git (port 443 implicit).
    A mirror on port 8443 of the same host must not be treated as origin — it is
    a genuinely different endpoint. The port-strip rule must only strip well-known
    defaults (22 for SSH, 443 for HTTPS), not all numeric ports.
    """
    repo, head = _repo_with_force_reset_reflog(tmp_path)
    _git(repo, "remote", "set-url", "origin", "https://github.com/org/repo.git")
    _git(repo, "remote", "add", "mirror8443", "https://github.com:8443/org/repo.git")
    proc = _run_hook(
        repo,
        f"refs/heads/master {head} refs/heads/master {head}\n",
        "mirror8443",
        "https://github.com:8443/org/repo.git",
    )
    assert FORCE_RESET_ERROR not in proc.stderr, proc.stderr


def test_force_reset_guard_applies_to_a_remote_whose_pushurl_is_origin(
    tmp_path: Path,
) -> None:
    """A push lands at `pushurl`, not `url` — the guard must follow it.

    `git remote add upstream <other>` + `git remote set-url --push upstream
    <origin>` is a real fork workflow: fetch from one place, push to another.
    Comparing fetch URLs made this remote look like a mirror and skipped the
    guard, even though the push rewrites origin's own history.
    """
    repo, head = _repo_with_force_reset_reflog(tmp_path)
    origin_url = _git(repo, "remote", "get-url", "origin").strip()
    _git(repo, "remote", "set-url", "upstream", "https://example.invalid/o/x.git")
    _git(repo, "remote", "set-url", "--push", "upstream", origin_url)
    proc = _run_hook(
        repo,
        f"refs/heads/master {head} refs/heads/master {head}\n",
        "upstream",
        origin_url,
    )
    assert FORCE_RESET_ERROR in proc.stderr, proc.stderr
    assert proc.returncode == 1


def test_mirror_push_is_still_skipped_when_pushurl_differs(tmp_path: Path) -> None:
    """The converse: a genuine mirror (pushurl elsewhere) still skips the guard."""
    repo, head = _repo_with_force_reset_reflog(tmp_path)
    _git(repo, "remote", "set-url", "mirror", "https://example.invalid/o/x.git")
    _git(
        repo, "remote", "set-url", "--push", "mirror", "https://example.invalid/o/y.git"
    )
    proc = _run_hook(
        repo,
        f"refs/heads/master {head} refs/heads/master {head}\n",
        "mirror",
        "https://example.invalid/o/y.git",
    )
    assert FORCE_RESET_ERROR not in proc.stderr, proc.stderr


def test_force_reset_guard_applies_when_the_remote_cannot_be_resolved(
    tmp_path: Path,
) -> None:
    """An unrecognised remote name is not evidence of a mirror. Fail closed."""
    repo, head = _repo_with_force_reset_reflog(tmp_path)
    proc = _run_hook(
        repo,
        f"refs/heads/master {head} refs/heads/master {head}\n",
        "never-configured",
        "git@example.com:o/x.git",
    )
    assert FORCE_RESET_ERROR in proc.stderr, proc.stderr


def test_force_reset_guard_fails_closed_when_origin_pushurl_differs(
    tmp_path: Path,
) -> None:
    """Guard exits 1 when origin.pushurl points somewhere different from origin.url.

    git fetch origin reads from the fetch URL and updates refs/remotes/origin/...
    for that endpoint. If origin.pushurl is a different repository, a force-reset
    on the push target is invisible to the reflog check — the guard would fail open.
    The fix: fail closed rather than silently using a stale reflog snapshot.
    """
    repo = _init(tmp_path)
    _git(repo, "remote", "set-url", "origin", "https://github.com/org/fetch-repo.git")
    _git(
        repo,
        "remote",
        "set-url",
        "--push",
        "origin",
        "https://github.com/org/push-repo.git",
    )
    head = _commit(repo, "README")
    proc = _run_hook(
        repo,
        f"refs/heads/master {head} refs/heads/master {head}\n",
        "origin",
        "https://github.com/org/push-repo.git",
    )
    assert (
        proc.returncode == 1
    ), f"expected exit 1 (fail closed), got {proc.returncode}\n{proc.stderr}"
    assert "pushurl" in proc.stderr or "push" in proc.stderr.lower(), proc.stderr


def test_force_reset_guard_runs_when_origin_pushurl_matches_fetchurl(
    tmp_path: Path,
) -> None:
    """Guard still applies when pushurl and fetchurl resolve to the same repository.

    A common configuration is an explicit pushurl that is just an alternate
    spelling of the same repo (e.g. SSH push URL vs HTTPS fetch URL). The guard
    must still run in this case — skipping it would defeat the protection for
    normal single-remote setups that configure both spellings.
    """
    repo, head = _repo_with_force_reset_reflog(tmp_path)
    # Same repo, different URL spellings — canonically identical.
    _git(repo, "remote", "set-url", "origin", "https://github.com/org/repo.git")
    _git(repo, "remote", "set-url", "--push", "origin", "git@github.com:org/repo.git")
    proc = _run_hook(
        repo,
        f"refs/heads/master {head} refs/heads/master {head}\n",
        "origin",
        "git@github.com:org/repo.git",
    )
    assert FORCE_RESET_ERROR in proc.stderr, proc.stderr
    assert proc.returncode == 1


def test_single_remote_new_branch_scopes_range_by_other_dest_refs(
    tmp_path: Path,
) -> None:
    """New-branch push in a single-remote clone must scope the range to new commits.

    Scenario: only origin is configured; origin/master already contains the base
    commit. A new topic branch has one mass-deletion commit on top. Without scoping,
    the range is all local history (2 commits > limit 1) and the guard skips the
    scan. With the fix, origin/master excludes the base commit so the range is just
    the deletion (1 commit, not > 1), the guard runs, and the push is blocked.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "remote", "add", "origin", str(tmp_path / "nonexistent.git"))

    # Seed files so the deletion commit exceeds the threshold.
    for name in ("a.txt", "b.txt", "c.txt"):
        (repo / name).write_text("x\n")
    _git(repo, "add", "a.txt", "b.txt", "c.txt")
    _git(repo, "commit", "--no-verify", "-qm", "seed")
    base = _git(repo, "rev-parse", "HEAD")
    # origin/master already published the seed commit.
    _git(repo, "update-ref", "refs/remotes/origin/master", base)

    # New topic branch: one mass-deletion on top of base.
    _git(repo, "rm", "-q", "a.txt", "b.txt", "c.txt")
    _git(repo, "commit", "--no-verify", "-qm", "delete everything")
    head = _git(repo, "rev-parse", "HEAD")

    # Limit 1: scoped range is just the deletion (1 commit, not > 1) → guard runs.
    # Without fix the range is all local history (2 commits > 1) → guard skips.
    proc = _run_hook(
        repo,
        f"refs/heads/topic {head} refs/heads/topic {ZERO}\n",
        "origin",
        "git@github.com:me/r.git",
        env={"MASS_DELETE_COMMIT_RANGE_LIMIT": "1", "MASS_DELETE_THRESHOLD": "1"},
    )
    assert SKIP_MESSAGE not in proc.stderr, proc.stderr
    assert proc.returncode == 1, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"


def test_bare_url_push_resolves_dest_remote_for_range_scoping(
    tmp_path: Path,
) -> None:
    """Pushing by bare URL must still scope the range using the matching remote's refs.

    Scenario: origin's push URL is the destination. The user runs
    `git push <origin-url> topic` (git passes the URL as argv[1], not the name
    "origin"). origin/master already holds the base commit. Without resolving
    the configured remote name from the URL, _dest_remote_name stays as the raw
    URL string and for-each-ref finds nothing, leaving the range unscoped. With
    the fix, the loop recognises that origin's push URL matches, sets
    _dest_remote_name="origin", and uses refs/remotes/origin/* for scoping.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    origin_url = str(tmp_path / "nonexistent.git")
    _git(repo, "remote", "add", "origin", origin_url)

    for name in ("a.txt", "b.txt", "c.txt"):
        (repo / name).write_text("x\n")
    _git(repo, "add", "a.txt", "b.txt", "c.txt")
    _git(repo, "commit", "--no-verify", "-qm", "seed")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/master", base)

    _git(repo, "rm", "-q", "a.txt", "b.txt", "c.txt")
    _git(repo, "commit", "--no-verify", "-qm", "delete everything")
    head = _git(repo, "rev-parse", "HEAD")

    # Pass origin_url as argv[1] (the remote "name") and again as argv[2] (URL),
    # simulating `git push <url> topic` with no configured remote name.
    proc = _run_hook(
        repo,
        f"refs/heads/topic {head} refs/heads/topic {ZERO}\n",
        origin_url,  # argv[1]: URL as remote name
        origin_url,  # argv[2]: URL
        env={"MASS_DELETE_COMMIT_RANGE_LIMIT": "1", "MASS_DELETE_THRESHOLD": "1"},
    )
    assert SKIP_MESSAGE not in proc.stderr, proc.stderr
    assert proc.returncode == 1, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"


def test_relative_remote_url_matches_absolute_destination(tmp_path: Path) -> None:
    """Relative-URL origin is recognised as the push destination.

    When origin is configured with a relative path (../nonexistent.git) and the
    push is made to the equivalent absolute URL, the hook must treat them as the
    same repository.  Before this fix, _canonical_remote_url left the paths
    distinct so origin entered _other_remotes; a stale refs/remotes/origin/topic
    at the destructive commit D was then excluded from the scan via
    ``--remotes=origin``, silently permitting the destructive re-push.
    """
    repo, base, head = _repo_with_mass_deletion(tmp_path)
    # repo is at tmp_path/repo; ../nonexistent.git resolves to tmp_path/nonexistent.git
    dest_abs = str(tmp_path / "nonexistent.git")
    _git(repo, "remote", "set-url", "origin", "../nonexistent.git")
    # Stale tracking ref at the destructive commit on the push destination.
    _git(repo, "update-ref", "refs/remotes/origin/topic", head)
    proc = _run_hook(
        repo,
        f"refs/heads/topic {head} refs/heads/topic {base}\n",
        dest_abs,
        dest_abs,
        env={"MASS_DELETE_COMMIT_RANGE_LIMIT": "0", "MASS_DELETE_THRESHOLD": "1"},
    )
    assert proc.returncode == 1, (
        "Guard must fire even when origin uses a relative-path URL alias.\n"
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


@pytest.mark.parametrize("remote_sha", [ZERO, "base"])
def test_bare_relative_alias_does_not_hide_destination_commit(
    tmp_path: Path, remote_sha: str
) -> None:
    """A plain relative path is a local URL too, even without ./ or ../."""
    repo, base, head = _repo_with_mass_deletion(tmp_path)
    destination = str(repo / "destination.git")
    _git(repo, "remote", "set-url", "origin", destination)
    _git(repo, "remote", "set-url", "backup", "destination.git")
    _git(repo, "update-ref", "refs/remotes/backup/topic", head)
    advertised = base if remote_sha == "base" else remote_sha
    proc = _run_hook(
        repo,
        f"refs/heads/topic {head} refs/heads/topic {advertised}\n",
        "origin",
        destination,
        env={"MASS_DELETE_COMMIT_RANGE_LIMIT": "0", "MASS_DELETE_THRESHOLD": "1"},
    )
    assert proc.returncode == 1, proc.stderr
    assert "PUSH BLOCKED: commit" in proc.stderr, proc.stderr


@pytest.mark.parametrize("remote_sha", [ZERO, "base"])
@pytest.mark.parametrize("file_url_destination", [False, True])
@pytest.mark.parametrize("filename", ["Destination.git", "My Repo%20:Name.git"])
def test_file_url_alias_does_not_hide_destination_commit(
    tmp_path: Path, remote_sha: str, file_url_destination: bool, filename: str
) -> None:
    """Local paths and file:// URLs must identify the same destination."""
    repo, base, head = _repo_with_mass_deletion(tmp_path)
    local_path = tmp_path / filename
    file_url = local_path.as_uri()
    configured, destination = (
        (str(local_path), file_url)
        if file_url_destination
        else (file_url, str(local_path))
    )
    _git(repo, "remote", "set-url", "origin", configured)
    _git(repo, "update-ref", "refs/remotes/origin/topic", head)
    advertised = base if remote_sha == "base" else remote_sha
    proc = _run_hook(
        repo,
        f"refs/heads/topic {head} refs/heads/topic {advertised}\n",
        destination,
        destination,
        env={"MASS_DELETE_COMMIT_RANGE_LIMIT": "0", "MASS_DELETE_THRESHOLD": "1"},
    )
    assert proc.returncode == 1, proc.stderr
    assert "PUSH BLOCKED: commit" in proc.stderr, proc.stderr


@pytest.mark.parametrize("remote_sha", [ZERO, "base"])
def test_secondary_push_url_alias_does_not_hide_destination_commit(
    tmp_path: Path, remote_sha: str
) -> None:
    """Every configured push destination participates in alias protection."""
    repo, base, head = _repo_with_mass_deletion(tmp_path)
    destination = _git(repo, "remote", "get-url", "origin")
    mirror = _git(repo, "remote", "get-url", "backup")
    _git(repo, "remote", "set-url", "--add", "--push", "backup", mirror)
    _git(repo, "remote", "set-url", "--add", "--push", "backup", destination)
    _git(repo, "update-ref", "refs/remotes/backup/topic", head)
    advertised = base if remote_sha == "base" else remote_sha
    proc = _run_hook(
        repo,
        f"refs/heads/topic {head} refs/heads/topic {advertised}\n",
        "origin",
        destination,
        env={"MASS_DELETE_COMMIT_RANGE_LIMIT": "0", "MASS_DELETE_THRESHOLD": "1"},
    )
    assert proc.returncode == 1, proc.stderr
    assert "PUSH BLOCKED: commit" in proc.stderr, proc.stderr


def test_head_prefixed_branch_scopes_new_branch_range(tmp_path: Path) -> None:
    """HEAD-feature is an ordinary branch, not the remote HEAD symbolic ref."""
    repo, base, head = _repo_with_mass_deletion(tmp_path)
    _git(repo, "update-ref", "refs/remotes/origin/HEAD-feature", base)
    destination = _git(repo, "remote", "get-url", "origin")
    proc = _run_hook(
        repo,
        f"refs/heads/topic {head} refs/heads/topic {ZERO}\n",
        "origin",
        destination,
        env={"MASS_DELETE_COMMIT_RANGE_LIMIT": "1", "MASS_DELETE_THRESHOLD": "1"},
    )
    assert SKIP_MESSAGE not in proc.stderr, proc.stderr
    assert proc.returncode == 1, proc.stderr
    assert "PUSH BLOCKED: commit" in proc.stderr, proc.stderr


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://FORGE.example/team/Repo.git", "forge.example/team/Repo"),
        ("git@FORGE.example:team/Repo.git", "forge.example/team/Repo"),
        ("ssh://git@FORGE.example/team/Repo.git", "forge.example/team/Repo"),
        ("https://GITHUB.com/Team/Repo.git", "github.com/team/repo"),
    ],
)
def test_network_identity_preserves_case_sensitive_repository_paths(
    url: str, expected: str
) -> None:
    """Only GitHub is known to have case-insensitive repository paths."""
    functions = HOOK.read_text().split(
        "# Read stdin before any subprocess consumes it."
    )[0]
    proc = subprocess.run(
        ["bash", "-c", functions + '\n_canonical_remote_url "$1"', "hook", url],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == expected


@pytest.mark.parametrize("scheme", ["https", "ssh"])
def test_case_sensitive_push_alias_does_not_borrow_wrong_tracking_baseline(
    tmp_path: Path, scheme: str
) -> None:
    """A fetch-only alias must not become the new-branch push baseline."""
    repo, base, head = _repo_with_mass_deletion(tmp_path)
    prefix = "https://forge.example/" if scheme == "https" else "git@forge.example:"
    destination = f"{prefix}team/repo.git"
    _git(repo, "remote", "add", "alpha", destination)
    _git(repo, "remote", "set-url", "--push", "alpha", f"{prefix}team/Repo.git")
    _git(repo, "remote", "add", "beta", destination)
    _git(repo, "update-ref", "refs/remotes/alpha/master", head)
    _git(repo, "update-ref", "refs/remotes/beta/master", base)
    proc = _run_hook(
        repo,
        f"refs/heads/topic {head} refs/heads/topic {ZERO}\n",
        destination,
        destination,
        env={"MASS_DELETE_COMMIT_RANGE_LIMIT": "0", "MASS_DELETE_THRESHOLD": "1"},
    )
    assert proc.returncode == 1, proc.stderr
    assert "PUSH BLOCKED: commit" in proc.stderr, proc.stderr


@pytest.mark.parametrize("nested_alias", [False, True])
def test_new_branch_does_not_borrow_nested_remote_baseline(
    tmp_path: Path, nested_alias: bool
) -> None:
    """for-each-ref's recursive prefix is not an exact remote namespace."""
    repo, base, head = _repo_with_mass_deletion(tmp_path)
    destination = _git(repo, "remote", "get-url", "origin")
    _git(repo, "remote", "add", "team", destination)
    nested_url = destination if nested_alias else str(tmp_path / "nested.git")
    # Newer git (2.55 on CI) refuses `remote add` for a nested name, but older
    # git and hand-edited config still produce one; write the config directly.
    _git(repo, "config", "remote.team/fork.url", nested_url)
    _git(
        repo,
        "config",
        "remote.team/fork.fetch",
        "+refs/heads/*:refs/remotes/team/fork/*",
    )
    # A real destination baseline keeps range limiting active; the nested
    # remote's stale topic must not hide the destructive new branch commit.
    _git(repo, "update-ref", "refs/remotes/team/master", base)
    _git(repo, "update-ref", "refs/remotes/team/fork/topic", head)
    proc = _run_hook(
        repo,
        f"refs/heads/topic {head} refs/heads/topic {ZERO}\n",
        "team",
        destination,
        env={"MASS_DELETE_COMMIT_RANGE_LIMIT": "1", "MASS_DELETE_THRESHOLD": "1"},
    )
    # The global pass already conservatively protects all nested namespaces
    # under the destination prefix, even when their URLs differ. The baseline
    # pass must not reintroduce those excluded refs through recursive traversal.
    assert SKIP_MESSAGE not in proc.stderr, proc.stderr
    assert proc.returncode == 1, proc.stderr
    assert "PUSH BLOCKED: commit" in proc.stderr, proc.stderr


@pytest.mark.parametrize("new_branch", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_absolute_ssh_alias_retains_destination_commits(
    tmp_path: Path, new_branch: bool, reverse: bool
) -> None:
    """SCP absolute paths and ssh:// paths describe the same destination."""
    repo, base, head = _repo_with_mass_deletion(tmp_path)
    destination, alias = "ssh://git@host/srv/r.git", "git@host:/srv/r.git"
    if reverse:
        destination, alias = alias, destination
    _git(repo, "remote", "set-url", "origin", destination)
    _git(repo, "remote", "set-url", "backup", alias)
    _git(repo, "update-ref", "refs/remotes/backup/topic", head)
    _git(repo, "update-ref", "refs/remotes/origin/master", base)
    proc = _run_hook(
        repo,
        f"refs/heads/topic {head} refs/heads/topic {ZERO if new_branch else base}\n",
        "origin",
        destination,
        env={"MASS_DELETE_COMMIT_RANGE_LIMIT": "0", "MASS_DELETE_THRESHOLD": "1"},
    )
    assert proc.returncode == 1, proc.stderr
    assert "PUSH BLOCKED: commit" in proc.stderr, proc.stderr


@pytest.mark.parametrize("new_branch", [False, True])
@pytest.mark.parametrize("exact", [False, True])
def test_destination_custom_fetch_namespace_is_not_another_remote(
    tmp_path: Path, new_branch: bool, exact: bool
) -> None:
    """A remote name does not establish ownership of its tracking namespace."""
    repo, base, head = _repo_with_mass_deletion(tmp_path)
    destination = _git(repo, "remote", "get-url", "origin")
    _git(repo, "remote", "add", "cache", str(tmp_path / "cache.git"))
    refspec = (
        "+refs/heads/topic:refs/remotes/cache/topic"
        if exact
        else "+refs/heads/*:refs/remotes/cache/*"
    )
    _git(repo, "config", "--add", "remote.origin.fetch", refspec)
    _git(repo, "update-ref", "refs/remotes/cache/topic", head)
    _git(repo, "update-ref", "refs/remotes/origin/master", base)
    proc = _run_hook(
        repo,
        f"refs/heads/topic {head} refs/heads/topic {ZERO if new_branch else base}\n",
        "origin",
        destination,
        env={"MASS_DELETE_COMMIT_RANGE_LIMIT": "0", "MASS_DELETE_THRESHOLD": "1"},
    )
    assert proc.returncode == 1, proc.stderr
    assert "PUSH BLOCKED: commit" in proc.stderr, proc.stderr
