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
    """A dirty-tree ff refusal must not clobber and must not retry-storm."""
    ws = Path("/tmp/ws")
    calls = []
    # Real git stderr when uncommitted local changes would be overwritten.
    dirty_stderr = (
        "error: Your local changes to the following files would be overwritten by merge:\n"
        "\tsome_file.py\n"
        "Please commit your changes or stash them before you merge.\n"
        "Aborting"
    )

    def fake_run(cmd, cwd=None, capture_output=None, text=None, check=None):
        calls.append(cmd)
        if len(cmd) > 1 and cmd[1] == "fetch":
            return _Run(0)
        return _Run(1, stderr=dirty_stderr)

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


def test_diverged_branch_ff_failure_returns_false():
    """A diverged branch (not a dirty-tree case) surfaces as False so callers know the workspace is stale."""
    ws = Path("/tmp/ws")
    calls = []
    # Real git stderr when the local branch has diverged from upstream.
    diverged_stderr = "fatal: Not possible to fast-forward, aborting."

    def fake_run(cmd, cwd=None, capture_output=None, text=None, check=None):
        calls.append(cmd)
        if len(cmd) > 1 and cmd[1] == "fetch":
            return _Run(0)
        return _Run(1, stderr=diverged_stderr)

    with patch("gptme_runloops.utils.git.subprocess.run", side_effect=fake_run):
        ok = git_pull_with_retry(ws, max_retries=3)

    # Diverged branch means the workspace was NOT updated — report False so
    # the caller can decide whether to proceed with potentially stale code.
    assert ok is False
    assert (
        len(calls) == 2
    )  # fetch + one ff-only attempt, no retry on structural failure


def test_lock_race_merge_failure_retries():
    """An index.lock race on merge retries; if all attempts hit the lock, returns False."""
    ws = Path("/tmp/ws")
    calls = []
    lock_stderr = "fatal: Unable to create '/tmp/ws/.git/index.lock': File exists."

    def fake_run(cmd, cwd=None, capture_output=None, text=None, check=None):
        calls.append(cmd)
        if len(cmd) > 1 and cmd[1] == "fetch":
            return _Run(0)
        return _Run(1, stderr=lock_stderr)

    with patch("gptme_runloops.utils.git.subprocess.run", side_effect=fake_run):
        ok = git_pull_with_retry(ws, max_retries=2, retry_delay=0)

    # All retries exhausted on a lock race — should report False (not a silent skip).
    assert ok is False
    # 2 retries = fetch+merge attempts: (fetch, merge) x2
    assert len(calls) == 4
