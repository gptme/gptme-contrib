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


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _run(repo: Path, *args: str, home: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HOME": str(home), "GIT_CONFIG_GLOBAL": os.devnull}
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


def test_hook_commits_without_stashing_unstaged_changes(
    repo: Path, tmp_path: Path
) -> None:
    assert _run(repo, home=tmp_path).returncode == 0
    (repo / "a.txt").write_text("a\n")
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "a")
    # The other session's untracked work is untouched by the hook.
    assert (repo / "b.txt").read_text() == "b\n"
    assert _git(repo, "stash", "list") == ""


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


def test_hook_path_is_home_relative_when_contrib_is_under_home(repo: Path) -> None:
    contrib_home = SCRIPT.parents[3]
    assert _run(repo, home=contrib_home).returncode == 0
    hook = (repo / ".git" / "hooks" / "pre-commit").read_text()
    relative = SCRIPT.with_name("pre-commit-auto-stage").relative_to(contrib_home)
    assert f'HOOK="$HOME/{relative}"' in hook


def test_installs_into_the_common_dir_from_a_linked_worktree(
    repo: Path, tmp_path: Path
) -> None:
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(worktree))
    assert _run(worktree, home=tmp_path).returncode == 0
    assert (repo / ".git" / "hooks" / "pre-commit").exists()


def test_rejects_non_repo(tmp_path: Path) -> None:
    result = _run(tmp_path, home=tmp_path)
    assert result.returncode == 2
    assert "not a git repository" in result.stderr
