"""The worktree push guard must run from a copied hook install, not only a checkout.

Fleet installs copy the hooks out of the volatile gptme-contrib working tree
into a non-git install root (``$ROOT/dotfiles/.config/git/hooks`` beside
``$ROOT/scripts/hooks``). The pre-push hook used to locate the guard only via
``git -C <hooks dir> rev-parse --show-toplevel``, which fails outside a
checkout, so the guard silently never ran for any push.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS_SRC = ROOT / "dotfiles" / ".config" / "git" / "hooks"

STUB_GUARD = """#!/usr/bin/env bash
cat > /dev/null
echo ran > "$GUARD_MARKER"
exit "${GUARD_EXIT:-0}"
"""


def _install(tmp_path: Path) -> Path:
    """Copy the hooks into a non-git install root with a stub guard beside them."""
    root = tmp_path / "install"
    hooks = root / "dotfiles" / ".config" / "git" / "hooks"
    shutil.copytree(HOOKS_SRC, hooks)
    guard = root / "scripts" / "hooks" / "worktree-push-guard"
    guard.parent.mkdir(parents=True)
    guard.write_text(STUB_GUARD)
    guard.chmod(0o755)
    return hooks / "pre-push"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for args in (
        ("config", "user.email", "t@t"),
        ("config", "user.name", "t"),
        ("remote", "add", "origin", str(tmp_path / "nonexistent.git")),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    return repo


def _push(hook: Path, repo: Path, marker: Path, guard_exit: str = "0"):
    env = {
        **os.environ,
        "GUARD_MARKER": str(marker),
        "GUARD_EXIT": guard_exit,
        "GIT_CEILING_DIRECTORIES": str(hook.parents[4]),
    }
    zero = "0" * 40
    return subprocess.run(
        [str(hook), "origin", "git@github.com:o/r.git"],
        cwd=repo,
        input=f"refs/heads/topic {zero} refs/heads/topic {zero}\n",
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def test_guard_runs_from_copied_install(tmp_path: Path) -> None:
    hook = _install(tmp_path)
    marker = tmp_path / "guard-ran"
    proc = _push(hook, _repo(tmp_path), marker)
    assert marker.exists(), proc.stderr
    assert proc.returncode == 0, proc.stderr


def test_guard_deny_blocks_from_copied_install(tmp_path: Path) -> None:
    hook = _install(tmp_path)
    marker = tmp_path / "guard-ran"
    proc = _push(hook, _repo(tmp_path), marker, guard_exit="1")
    assert marker.exists(), proc.stderr
    assert proc.returncode == 1


def test_guard_in_pushed_repo_is_never_executed(tmp_path: Path) -> None:
    """The fallback must not resolve against the repository being pushed."""
    hook = _install(tmp_path)
    (hook.parents[4] / "scripts" / "hooks" / "worktree-push-guard").unlink()
    repo = _repo(tmp_path)
    planted = repo / "scripts" / "hooks" / "worktree-push-guard"
    planted.parent.mkdir(parents=True)
    planted.write_text(STUB_GUARD)
    planted.chmod(0o755)
    marker = tmp_path / "guard-ran"
    proc = _push(hook, repo, marker)
    assert not marker.exists(), proc.stderr
    assert proc.returncode == 0, proc.stderr
