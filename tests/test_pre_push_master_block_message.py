"""The pre-push master block must not advertise ``--no-verify`` as the way out.

Incident (gptme/gptme-cloud, 2026-10-13): a worker's feature branch tracked
``origin/master``. Under ``push.default=upstream``, ``git push origin BRANCH``
targeted master; the hook blocked it and printed "If intentional, use: git push
--no-verify". The agent ran exactly that 8 seconds later and landed the commit on
master. The block message must instead name the tracking trap and give the
explicit refspec.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "dotfiles" / ".config" / "git" / "hooks" / "pre-push"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "remote", "add", "origin", str(tmp_path / "nonexistent.git"))
    (repo / "README").write_text("x\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "--no-verify", "-qm", "init")
    return repo, _git(repo, "rev-parse", "HEAD")


def _run_hook(repo: Path, stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(HOOK), "origin", "git@github.com:example-org/example-repo.git"],
        cwd=repo,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        env=dict(os.environ),
    )


def test_feature_branch_to_master_names_the_trap(tmp_path: Path) -> None:
    """Without upstream tracking configured, only the generic refspec is shown."""
    repo, head = _repo(tmp_path)
    proc = _run_hook(
        repo, f"refs/heads/fix/thing {head} refs/heads/master {'0' * 40}\n"
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "Direct push to refs/heads/master blocked" in out
    assert "git push -u origin fix/thing:refs/heads/fix/thing" in out
    assert "If intentional, use: git push --no-verify" not in out
    assert "Do NOT retry with --no-verify" in out
    # Without actual tracking, the hook must NOT claim "it tracks origin/master".
    assert "It tracks origin/master" not in out


def test_feature_branch_with_tracking_names_the_upstream(tmp_path: Path) -> None:
    """When the branch actually tracks origin/master, the message says so."""
    repo, head = _repo(tmp_path)
    # Configure the feature branch to track origin/master (the real incident shape).
    _git(repo, "config", "branch.fix/thing.remote", "origin")
    _git(repo, "config", "branch.fix/thing.merge", "refs/heads/master")
    proc = _run_hook(
        repo, f"refs/heads/fix/thing {head} refs/heads/master {'0' * 40}\n"
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "It tracks origin/master" in out
    assert "git push -u origin fix/thing:refs/heads/fix/thing" in out
    assert "Do NOT retry with --no-verify" in out


def test_master_to_master_suggests_a_branch(tmp_path: Path) -> None:
    repo, head = _repo(tmp_path)
    proc = _run_hook(repo, f"refs/heads/master {head} refs/heads/master {'0' * 40}\n")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "git switch -c feature-branch" in out
    assert "is being pushed to" not in out
    assert "If intentional, use: git push --no-verify" not in out
