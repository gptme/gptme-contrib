"""The shared pre-commit hooks must never invoke a bare `prek run`.

Without `--files`, prek (and pre-commit) stash every unstaged change in the
worktree and restore it after the hooks, overwriting anything a concurrent
process wrote meanwhile. Deletion-only and empty commits used to hit that bare
branch because the hooks built their file list with `--diff-filter=ACM`.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = [
    REPO_ROOT / "scripts" / "git" / "pre-commit-auto-stage",
    REPO_ROOT / "dotfiles" / ".config" / "git" / "hooks" / "pre-commit",
]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "feature")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "core.hooksPath", "/dev/null")
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n")
    (repo / "gone.txt").write_text("gone\n")
    (repo / "sibling.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    # A concurrent session's uncommitted edit that a stash would capture.
    (repo / "sibling.txt").write_text("sibling edit\n")
    return repo


def _run_hook(hook: Path, repo: Path, tmp_path: Path) -> list[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "prek-args.log"
    fake_prek = fake_bin / "prek"
    fake_prek.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexit 0\n')
    fake_prek.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["ALLOW_GIT_IDENTITY"] = "1"  # test repo identity; dotfiles hook guards it
    result = subprocess.run(
        [str(hook)], cwd=repo, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return log.read_text().splitlines()


@pytest.mark.parametrize("hook", HOOKS, ids=lambda p: p.name)
def test_deletion_only_commit_passes_files(hook: Path, repo: Path, tmp_path: Path):
    _git(repo, "rm", "-q", "gone.txt")
    calls = _run_hook(hook, repo, tmp_path)
    assert calls == ["run --files gone.txt"]


@pytest.mark.parametrize("hook", HOOKS, ids=lambda p: p.name)
def test_empty_commit_passes_placeholder(hook: Path, repo: Path, tmp_path: Path):
    calls = _run_hook(hook, repo, tmp_path)
    assert len(calls) == 1
    assert calls[0].startswith("run --files ")
    placeholder = calls[0].removeprefix("run --files ")
    assert not (repo / placeholder).exists()
