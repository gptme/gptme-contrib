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
    """The fallback must resolve the install root, never the pushed repo.

    Both guards exist and are executable, and they write different markers, so
    the assertion distinguishes "the install guard ran" from "the planted one
    ran" instead of only proving that a missing guard is not executed.
    """
    hook = _install(tmp_path)
    install_marker = tmp_path / "install-guard-ran"
    install_guard = hook.parents[4] / "scripts" / "hooks" / "worktree-push-guard"
    install_guard.write_text(
        STUB_GUARD.replace(
            'echo ran > "$GUARD_MARKER"', f'echo ran > "{install_marker}"'
        )
    )
    repo = _repo(tmp_path)
    planted = repo / "scripts" / "hooks" / "worktree-push-guard"
    planted.parent.mkdir(parents=True)
    planted.write_text(STUB_GUARD)
    planted.chmod(0o755)
    repo_marker = tmp_path / "repo-guard-ran"
    proc = _push(hook, repo, repo_marker)
    assert install_marker.exists(), proc.stderr
    assert not repo_marker.exists(), proc.stderr
    assert proc.returncode == 0, proc.stderr


def test_guard_outside_install_layout_is_never_executed(tmp_path: Path) -> None:
    """A hook in <superproject>/.git/modules/<sub>/hooks must not reach the
    superproject working tree four levels up."""
    superproject = tmp_path / "superproject"
    hooks = superproject / ".git" / "modules" / "sub" / "hooks"
    shutil.copytree(HOOKS_SRC, hooks)
    planted = superproject / "scripts" / "hooks" / "worktree-push-guard"
    planted.parent.mkdir(parents=True)
    planted.write_text(STUB_GUARD)
    planted.chmod(0o755)
    marker = tmp_path / "guard-ran"
    proc = _push(hooks / "pre-push", _repo(tmp_path), marker)
    assert not marker.exists(), proc.stderr
    assert proc.returncode == 0, proc.stderr


def test_guard_gitdir_path_matching_install_suffix_is_never_executed(
    tmp_path: Path,
) -> None:
    """A submodule gitdir can end in ``/dotfiles/.config/git/hooks`` too.

    ``<super>/.git/modules/dotfiles/.config/git`` is the gitdir of a submodule
    at ``dotfiles/.config/git``, so its hooks dir matches the copied-install
    suffix without being an install root. The fallback must refuse a root
    inside a git directory rather than execute whatever sits beside it.
    """
    superproject = tmp_path / "superproject"
    hooks = superproject / ".git" / "modules" / "dotfiles" / ".config" / "git" / "hooks"
    shutil.copytree(HOOKS_SRC, hooks)
    # Where an unguarded fallback would land: the parent of the modules dir.
    derived_root = superproject / ".git" / "modules"
    planted = derived_root / "scripts" / "hooks" / "worktree-push-guard"
    planted.parent.mkdir(parents=True)
    planted.write_text(STUB_GUARD)
    planted.chmod(0o755)
    marker = tmp_path / "guard-ran"
    proc = _push(hooks / "pre-push", _repo(tmp_path), marker)
    assert not marker.exists(), proc.stderr
    assert proc.returncode == 0, proc.stderr


def _git(repo: Path, *args: str) -> str:
    """Run git with the launcher's own repository-locating env removed."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env=_push_env(),
    ).stdout.strip()


def _push_env(**extra: str) -> dict[str, str]:
    """A clean git environment: the caller's GIT_* must not steer fixtures.

    The inherited-environment bug is reproduced by *git itself* exporting
    GIT_DIR to the hook, so the fixture environment must start neutral.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
    )
    env.update(extra)
    return env


def _push_from_worktree(
    tmp_path: Path,
    hook: Path,
    marker: Path,
    guard_exit: str = "0",
    plant_repo_guard: bool = False,
):
    """Push for real, from a linked worktree, to a local bare destination.

    Git runs pre-push with the pushing repository's local environment exported.
    For a linked worktree that includes an absolute ``GIT_DIR``; the hook must
    not let that inherited value steer hook-location discovery. The hook set is
    wired through the pushed repository's own ``core.hooksPath``, so no global
    install is touched.

    ``plant_repo_guard`` plants an executable guard in the *linked worktree*
    before the push. The inherited ``GIT_DIR`` points at that worktree's gitdir,
    so an over-broad discovery resolves the worktree root — planting there is
    what makes the hostile-repo assertion meaningful.
    """
    destination = tmp_path / "destination.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(destination)],
        check=True,
        env=_push_env(),
        timeout=60,
    )
    repo = tmp_path / "push-repo"
    subprocess.run(
        ["git", "init", "-q", "-b", "topic", str(repo)],
        check=True,
        env=_push_env(),
        timeout=60,
    )
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "remote", "add", "origin", str(destination))
    (repo / "file.txt").write_text("x\n")
    _git(repo, "add", "file.txt")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "initial")
    worktree = tmp_path / "push-worktree"
    _git(
        repo,
        "-c",
        "core.hooksPath=/dev/null",
        "worktree",
        "add",
        "-q",
        "-b",
        "pushed",
        str(worktree),
    )
    # Repo-local config is shared by the linked worktree.
    _git(repo, "config", "core.hooksPath", str(hook.parent))
    if plant_repo_guard:
        # The pushing cwd/worktree root is what inherited GIT_DIR resolves to.
        planted = worktree / "scripts" / "hooks" / "worktree-push-guard"
        planted.parent.mkdir(parents=True)
        planted.write_text(STUB_GUARD)
        planted.chmod(0o755)
    return subprocess.run(
        ["git", "push", "origin", "HEAD:refs/heads/topic"],
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=120,
        env=_push_env(GUARD_MARKER=str(marker), GUARD_EXIT=guard_exit),
    )


def test_guard_runs_on_real_push_from_copied_install(tmp_path: Path) -> None:
    """A real ``git push`` exports GIT_DIR; discovery must still reach the
    copied install's guard instead of resolving the hooks dir as a repo."""
    hook = _install(tmp_path)
    marker = tmp_path / "guard-ran"
    proc = _push_from_worktree(tmp_path, hook, marker)
    assert proc.returncode == 0, proc.stderr
    assert marker.exists(), proc.stderr


def test_guard_deny_blocks_real_push_from_copied_install(tmp_path: Path) -> None:
    hook = _install(tmp_path)
    marker = tmp_path / "guard-ran"
    proc = _push_from_worktree(tmp_path, hook, marker, guard_exit="1")
    assert marker.exists(), proc.stderr
    assert proc.returncode != 0, proc.stderr


def test_guard_in_pushed_repo_is_never_executed_on_real_push(tmp_path: Path) -> None:
    """Inherited GIT_DIR must not point discovery at the pushed repository."""
    hook = _install(tmp_path)
    install_marker = tmp_path / "install-guard-ran"
    install_guard = hook.parents[4] / "scripts" / "hooks" / "worktree-push-guard"
    install_guard.write_text(
        STUB_GUARD.replace(
            'echo ran > "$GUARD_MARKER"', f'echo ran > "{install_marker}"'
        )
    )
    repo_marker = tmp_path / "repo-guard-ran"
    proc = _push_from_worktree(tmp_path, hook, repo_marker, plant_repo_guard=True)
    # The hostile guard sits exactly where naive discovery would look: the
    # linked worktree root, which is what the pushed repository's own gitdir
    # names as its worktree.
    worktree = tmp_path / "push-worktree"
    plant = worktree / "scripts" / "hooks" / "worktree-push-guard"
    assert _git(worktree, "rev-parse", "--show-toplevel") == str(worktree)
    assert plant.is_file(), proc.stderr
    assert plant.stat().st_mode & 0o100, "planted guard is not executable"
    assert install_marker.exists(), proc.stderr
    assert not repo_marker.exists(), proc.stderr
    assert proc.returncode == 0, proc.stderr
