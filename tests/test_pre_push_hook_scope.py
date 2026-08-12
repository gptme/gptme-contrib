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
