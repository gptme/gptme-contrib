"""Tests for shared-worktree-safe git_pull_with_retry.

The run-item executor calls ``git_pull_with_retry`` on the shared brain
workspace (/home/bob/bob) during ``pre_run``. A bare ``git pull`` there is a
tree-wide operation that can clobber or orphan other sessions' uncommitted
edits (the recurring clobber-canary incidents). The fix: fetch + ``merge
--ff-only``, which never stashes, never rebases, never creates a merge commit,
and refuses rather than overwriting when local changes overlap the incoming
diff. These tests lock in that behavior contract.
"""

from pathlib import Path
from unittest.mock import patch

from gptme_runloops.utils.git import git_pull_with_retry


class _Run:
    """A subprocess.run return-value stand-in."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_uses_fetch_then_ff_only_not_bare_pull():
    """The shared-tree pull must fetch then fast-forward, never bare ``git pull``."""
    ws = Path("/tmp/ws")
    seen = []

    def fake_run(cmd, cwd=None, capture_output=None, text=None, check=None):
        seen.append(cmd)
        # fetch succeeds, merge fast-forwards
        return _Run(0)

    with patch("gptme_runloops.utils.git.subprocess.run", side_effect=fake_run):
        ok = git_pull_with_retry(ws, max_retries=1)

    assert ok is True
    assert seen[0] == ["git", "fetch", "--quiet"]
    assert seen[1] == ["git", "merge", "--ff-only"]
    # Assert a bare `git pull` (the clobber source) is never invoked.
    assert all(len(cmd) < 2 or cmd[1] != "pull" for cmd in seen)


def test_dirty_tree_ff_skip_returns_true_and_does_not_retry():
    """A blocked fast-forward (dirty overlap) must not clobber and must not retry-storm."""
    ws = Path("/tmp/ws")
    calls = []

    def fake_run(cmd, cwd=None, capture_output=None, text=None, check=None):
        calls.append(cmd)
        if len(cmd) > 1 and cmd[1] == "fetch":
            return _Run(0)
        # merge --ff-only refuses (dirty local overlap / divergence)
        return _Run(1, stderr="error: cannot fast-forward")

    with patch("gptme_runloops.utils.git.subprocess.run", side_effect=fake_run):
        ok = git_pull_with_retry(ws, max_retries=3)

    # The dirty tree is the SAFE outcome: nothing was clobbered, pre_run may
    # continue, and we do not hammer retries on a non-transient refusal.
    assert ok is True
    assert len(calls) == 2  # fetch + one ff-only, no retry loop


def test_fetch_failure_retries_then_returns_false():
    """A persistent fetch failure retries then returns False (caller may skip run)."""
    ws = Path("/tmp/ws")
    calls = []

    def fake_run(cmd, cwd=None, capture_output=None, text=None, check=None):
        calls.append(cmd)
        if len(cmd) > 1 and cmd[1] == "fetch":
            return _Run(1, stderr="could not connect")
        return _Run(0)

    with patch("gptme_runloops.utils.git.subprocess.run", side_effect=fake_run):
        ok = git_pull_with_retry(ws, max_retries=2, retry_delay=0)

    assert ok is False
    assert len(calls) == 2  # exactly two fetch attempts (max_retries)


def test_ff_failure_after_retryable_fetch_does_not_retry():
    """A successful fetch followed by a refused ff-only returns True immediately."""
    ws = Path("/tmp/ws")
    calls = []

    def fake_run(cmd, cwd=None, capture_output=None, text=None, check=None):
        calls.append(cmd)
        if len(cmd) > 1 and cmd[1] == "fetch":
            return _Run(0)
        return _Run(1, stderr="cannot fast-forward")

    with patch("gptme_runloops.utils.git.subprocess.run", side_effect=fake_run):
        ok = git_pull_with_retry(ws, max_retries=3)

    assert ok is True
    assert len(calls) == 2  # no retry on a clean fast-forward refusal
