"""Git operations with retry logic (shared-worktree-safe)."""

import logging
import subprocess
import time
from pathlib import Path


# Why fast-forward only, never a plain ``git pull``:
# ``git_pull_with_retry`` runs on the shared brain workspace (/home/bob/bob),
# which is written concurrently by ~20 sessions. A plain ``git pull`` in a
# dirty tree is a tree-wide operation: with the merge default (pull.rebase
# unset/false) it can overwrite or orphan other sessions' uncommitted edits,
# and any autostash variant snapshots the WHOLE tree and drops the pop on a
# raced rebase. The brain repo already hardened its own pull path to
# ``git-safe-pull`` (fast-forward only, takes the commit flock, never stashes)
# — see scripts/git/git-safe-pull and scripts/util/git-pull.sh. This package
# default replicates the same guarantee for runloops run-item sessions, which
# call this hook on the shared tree during pre_run.
#
# ``fetch`` + ``merge --ff-only`` never creates a merge commit, never stashes,
# and refuses (rather than overwriting) when local changes overlap the incoming
# diff. A sibling's dirty file is left untouched; the pull simply doesn't land
# this cycle, and the retry loop still gives transient failures a few chances.
def git_pull_with_retry(
    workspace: Path,
    max_retries: int = 3,
    retry_delay: int = 5,
    logger: logging.Logger | None = None,
) -> bool:
    """Pull latest changes from git with retry logic.

    Fast-forward only: never stashes, never rebases, never merges. If the
    local tree is dirty in a way that blocks a clean fast-forward, the pull is
    skipped rather than clobbering other sessions' uncommitted work.

    Args:
        workspace: Git repository path
        max_retries: Maximum number of retry attempts
        retry_delay: Seconds to wait between retries
        logger: Optional logger for messages

    Returns:
        True if the fast-forward succeeded, there was nothing to pull,
        or the fast-forward was refused because of local uncommitted changes
        (safe skip — nothing was clobbered).
        False if the fetch failed after all retries (network error),
        or if the merge failed for a structural reason (diverged branch,
        no upstream configured, lock-file race exhausting all retries).
    """

    def log(msg: str) -> None:
        if logger:
            logger.info(msg)
        else:
            print(msg)

    for attempt in range(1, max_retries + 1):
        try:
            # Fetch first; a failed fetch (network) should retry.
            fetch = subprocess.run(
                ["git", "fetch", "--quiet"],
                cwd=workspace,
                capture_output=True,
                text=True,
            )
            if fetch.returncode != 0:
                raise subprocess.CalledProcessError(
                    fetch.returncode, ["git", "fetch"], stderr=fetch.stderr
                )
            # Fast-forward the current branch onto its upstream, refusing to
            # clobber local uncommitted changes or create a merge commit.
            merge = subprocess.run(
                ["git", "merge", "--ff-only"],
                cwd=workspace,
                capture_output=True,
                text=True,
            )
            if merge.returncode != 0:
                stderr = merge.stderr.strip()
                # Dirty-tree refusal: local uncommitted changes block the
                # fast-forward. On the shared worktree this is the SAFE
                # outcome — nothing was clobbered. Return True so pre_run
                # continues with the current local state.
                if (
                    "overwritten by merge" in stderr
                    or "Please commit your changes" in stderr
                ):
                    log(
                        f"Git fast-forward skipped (dirty tree, "
                        f"attempt {attempt}/{max_retries}): {stderr}"
                    )
                    return True
                # Transient lock-file race (common in a 20-session shared
                # worktree): retry rather than declaring failure.
                if "index.lock" in stderr or "Unable to create" in stderr:
                    log(
                        f"WARNING: Git merge lock race (attempt "
                        f"{attempt}/{max_retries}), retrying in {retry_delay}s..."
                    )
                    if attempt < max_retries:
                        time.sleep(retry_delay)
                        continue
                # Structural failure (diverged branch, no upstream, etc.):
                # the local branch was NOT updated — surface as False so
                # callers know the workspace may be stale.
                log(
                    f"Git fast-forward failed (attempt {attempt}/{max_retries}): "
                    f"{stderr or 'not a clean fast-forward'}"
                )
                return False
            log(f"Git pull successful (attempt {attempt}/{max_retries})")
            return True

        except subprocess.CalledProcessError:
            if attempt < max_retries:
                log(
                    f"WARNING: Git fetch failed (attempt {attempt}/{max_retries}), "
                    f"retrying in {retry_delay}s..."
                )
                time.sleep(retry_delay)
            else:
                log(
                    f"ERROR: Git fetch failed after {max_retries} attempts, "
                    "continuing with current state"
                )
                return False

    return False
