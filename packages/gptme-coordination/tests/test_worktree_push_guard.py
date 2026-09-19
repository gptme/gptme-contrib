"""Smoke tests for the worktree push guard (contrib port, ErikBjare/alice#83)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from gptme_coordination.worktree_guard import (
    legacy_pr_branch_key,
    origin_slug,
    pushed_branches,
)

CONTRIB_ROOT = Path(__file__).resolve().parents[3]


def test_legacy_key_unqualifies_repo_qualified_branch_claim() -> None:
    assert legacy_pr_branch_key("pr-branch:org/repo#feat") == "pr-branch:feat"
    assert legacy_pr_branch_key("pr-branch:feat") is None
    assert legacy_pr_branch_key("github:org/repo#1") is None


def test_origin_slug() -> None:
    assert origin_slug("git@github.com:org/repo.git") == "org/repo"
    assert origin_slug("https://github.com/org/repo") == "org/repo"
    assert origin_slug("https://gitlab.com/org/repo.git") is None


def test_pushed_branches_filters_deletes_and_non_heads() -> None:
    lines = [
        "refs/heads/feat abc123 refs/heads/feat def456",
        "refs/heads/gone " + "0" * 40 + " refs/heads/gone def456",  # deletion
        "refs/tags/v1 abc123 refs/tags/v1 def456",  # not a branch
    ]
    assert pushed_branches(lines) == ["feat"]


def test_entry_point_fails_open_without_package() -> None:
    """Guard entry point exits 0 when the coordination package is missing."""
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}
    result = subprocess.run(
        [sys.executable, str(CONTRIB_ROOT / "scripts/hooks/worktree-push-guard")],
        input="refs/heads/feat abc refs/heads/feat def\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
