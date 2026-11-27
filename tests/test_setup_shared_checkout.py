"""Tests for scripts/git/setup-shared-checkout.sh."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "git"
    / "setup-shared-checkout.sh"
)

pytestmark = pytest.mark.skipif(
    shutil.which("flock") is None, reason="flock is required by the commit hook"
)


# Global/system git config (core.hooksPath, init.templateDir) must not change
# which hook runs, for setup and for the git calls the tests make.
_ISOLATED = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_ISOLATED},
    ).stdout.strip()


def _run(repo: Path, *args: str, home: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HOME": str(home), **_ISOLATED}
    return subprocess.run(
        ["bash", str(SCRIPT), *args, str(repo)],
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    return path


def test_check_reports_missing_then_setup_fixes_everything(
    repo: Path, tmp_path: Path
) -> None:
    before = _run(repo, "--check", home=tmp_path)
    assert before.returncode == 1
    assert "missing: git config safety.sharedCheckout true" in before.stdout

    setup = _run(repo, home=tmp_path)
    assert setup.returncode == 0, setup.stderr
    assert _git(repo, "config", "--bool", "safety.sharedCheckout") == "true"
    assert _git(repo, "config", "pull.ff") == "only"
    assert _git(repo, "config", "rebase.autoStash") == "false"
    assert _git(repo, "config", "merge.autoStash") == "false"

    after = _run(repo, "--check", home=tmp_path)
    assert after.returncode == 0, after.stdout
    assert "ok:" in after.stdout


@pytest.mark.skipif(
    shutil.which("prek") is None and shutil.which("pre-commit") is None,
    reason="needs prek or pre-commit to run a hook",
)
def test_hook_runs_hooks_without_stashing_unstaged_changes(
    repo: Path, tmp_path: Path
) -> None:
    """While hooks run, another session's unstaged edit stays in the tree.

    A plain `prek install` / `pre-commit install` hook stashes unstaged changes
    around the run; the probe hook below would then not see the edit.
    """
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: sees-unstaged-edit\n"
        "        name: sees unstaged edit\n"
        "        entry: sh -c 'grep -q other-session-edit shared.txt && touch hook-saw-edit'\n"
        "        language: system\n"
        "        pass_filenames: false\n"
        "        always_run: true\n"
    )
    (repo / "shared.txt").write_text("committed\n")
    _git(repo, "add", ".pre-commit-config.yaml", "shared.txt")
    _git(repo, "commit", "-q", "--no-verify", "-m", "init")
    assert _run(repo, home=tmp_path).returncode == 0

    (repo / "shared.txt").write_text("committed\nother-session-edit\n")  # unstaged
    (repo / "mine.txt").write_text("mine\n")
    _git(repo, "add", "mine.txt")
    _git(repo, "commit", "-q", "-m", "mine")

    assert (repo / "hook-saw-edit").exists(), "hooks ran on a stashed tree"
    assert "other-session-edit" in (repo / "shared.txt").read_text()
    assert _git(repo, "stash", "list") == ""
    assert _git(repo, "show", "--name-only", "--format=", "HEAD") == "mine.txt"


def test_git_safe_commit_accepts_the_installed_wrapper(
    repo: Path, tmp_path: Path
) -> None:
    """git-safe-commit relaxes its untracked-files guard only for hooks it
    knows don't stash; the setup wrapper must count as one."""
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    assert _run(repo, home=tmp_path).returncode == 0
    (repo / "other-session-new-file.txt").write_text("not mine\n")
    (repo / "mine.txt").write_text("mine\n")
    result = subprocess.run(
        [str(SCRIPT.with_name("git-safe-commit")), "mine.txt", "-m", "mine"],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path), **_ISOLATED},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(repo, "show", "--name-only", "--format=", "HEAD") == "mine.txt"
    assert (repo / "other-session-new-file.txt").exists()


def test_existing_hook_is_kept_and_rerun_is_idempotent(
    repo: Path, tmp_path: Path
) -> None:
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n")

    assert _run(repo, home=tmp_path).returncode == 0
    backups = list(hooks.glob("pre-commit.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "#!/bin/sh\nexit 0\n"

    rerun = _run(repo, home=tmp_path)
    assert rerun.returncode == 0
    assert "installed" not in rerun.stdout
    assert len(list(hooks.glob("pre-commit.bak.*"))) == 1


def test_hook_path_is_home_relative_when_contrib_is_under_home(
    repo: Path, tmp_path: Path
) -> None:
    # A copy of the scripts under a temporary HOME, independent of where this
    # checkout lives (CI may clone to /src, whose parent is /).
    home = tmp_path / "home"
    scripts = home / "src" / "gptme-contrib" / "scripts" / "git"
    scripts.mkdir(parents=True)
    for name in ("setup-shared-checkout.sh", "pre-commit-auto-stage"):
        shutil.copy2(SCRIPT.with_name(name), scripts / name)
    env = {**os.environ, "HOME": str(home), **_ISOLATED}
    result = subprocess.run(
        ["bash", str(scripts / "setup-shared-checkout.sh"), str(repo)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    hook = (repo / ".git" / "hooks" / "pre-commit").read_text()
    assert 'HOOK="$HOME/src/gptme-contrib/scripts/git/pre-commit-auto-stage"' in hook


def test_hook_path_is_absolute_outside_home(repo: Path, tmp_path: Path) -> None:
    home = tmp_path / "elsewhere"
    home.mkdir()
    assert _run(repo, home=home).returncode == 0
    hook = (repo / ".git" / "hooks" / "pre-commit").read_text()
    assert f'HOOK="{SCRIPT.with_name("pre-commit-auto-stage")}"' in hook


def test_installs_into_the_common_dir_from_a_linked_worktree(
    repo: Path, tmp_path: Path
) -> None:
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(worktree))
    assert _run(worktree, home=tmp_path).returncode == 0
    assert (repo / ".git" / "hooks" / "pre-commit").exists()


def test_hooks_path_makes_setup_fail_loudly(repo: Path, tmp_path: Path) -> None:
    _git(repo, "config", "core.hooksPath", ".githooks")
    result = _run(repo, home=tmp_path)
    assert result.returncode == 1
    assert "core.hooksPath" in result.stderr
    # The config part still applied.
    assert _git(repo, "config", "--bool", "safety.sharedCheckout") == "true"


def test_rejects_non_repo(tmp_path: Path) -> None:
    result = _run(tmp_path, home=tmp_path)
    assert result.returncode == 2
    assert "not a git repository" in result.stderr
