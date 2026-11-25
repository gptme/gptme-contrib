"""Git worktree management for isolated agent execution.

Provides worktree creation, cleanup, and PR/merge workflows for agent isolation.
This enables multiple agents to work on different features simultaneously
without conflicts.

Implements Issue #246: worktree workflow for isolated agent execution.
"""

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


@dataclass
class WorktreeInfo:
    """Information about a git worktree."""

    path: Path
    branch: str
    task_id: str
    base_branch: str = "origin/master"
    status: Literal["active", "merged", "pr_created", "removed"] = "active"


def get_worktrees_dir(workspace: Path | None = None) -> Path:
    """Get the worktrees directory, creating if needed.

    Worktrees are stored in .worktrees/ by default to keep them
    separate from regular project files.
    """
    if workspace is None:
        workspace = Path.cwd()
    worktrees_dir = workspace / ".worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    return worktrees_dir


def _run_git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a git command and return the result."""
    cmd = ["git"] + args
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def create_worktree(
    task_id: str,
    branch_name: str | None = None,
    base_branch: str = "origin/master",
    workspace: Path | None = None,
) -> WorktreeInfo:
    """Create a git worktree for isolated agent execution.

    Args:
        task_id: The task ID being worked on (used for naming)
        branch_name: Branch name for the worktree (default: task-{task_id})
        base_branch: Base branch to branch from (default: origin/master)
        workspace: Root workspace directory

    Returns:
        WorktreeInfo with path and branch details

    Raises:
        RuntimeError: If worktree creation fails
    """
    if workspace is None:
        workspace = Path.cwd()

    if branch_name is None:
        # Sanitize task_id for use as branch name
        safe_id = task_id.replace("/", "-").replace(" ", "-").lower()
        branch_name = f"task-{safe_id}"

    worktrees_dir = get_worktrees_dir(workspace)
    worktree_path = worktrees_dir / branch_name

    # Check if worktree already exists
    if worktree_path.exists():
        logger.warning(f"Worktree already exists at {worktree_path}")
        return WorktreeInfo(
            path=worktree_path,
            branch=branch_name,
            task_id=task_id,
            base_branch=base_branch,
            status="active",
        )

    # Fetch latest from origin to ensure base_branch is up to date
    # Extract the ref to fetch from base_branch (e.g. "origin/master" -> "master")
    # FIX #1: Use split("/")[-1] to get the last component correctly
    # Handles "refs/heads/main" -> "main" (was incorrectly producing "heads/main")
    fetch_ref = base_branch.split("/")[-1] if "/" in base_branch else base_branch
    result = _run_git(["fetch", "origin", fetch_ref], cwd=workspace)
    if result.returncode != 0:
        logger.warning(f"Failed to fetch: {result.stderr}")

    # Create worktree with new branch
    result = _run_git(
        ["worktree", "add", str(worktree_path), "-b", branch_name, base_branch],
        cwd=workspace,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Failed to create worktree: {result.stderr}")

    # FIX #2: Check if upstream exists before unsetting to prevent stderr noise
    # Unset upstream to prevent accidental push to base branch
    upstream_result = _run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], cwd=worktree_path
    )
    if upstream_result.returncode == 0 and upstream_result.stdout.strip():
        _run_git(["branch", "--unset-upstream"], cwd=worktree_path)

    logger.info(f"Created worktree at {worktree_path} on branch {branch_name}")

    return WorktreeInfo(
        path=worktree_path,
        branch=branch_name,
        task_id=task_id,
        base_branch=base_branch,
        status="active",
    )


def list_worktrees(workspace: Path | None = None) -> list[dict]:
    """List all git worktrees in the repository.

    Returns list of dicts with worktree info from git.
    """
    if workspace is None:
        workspace = Path.cwd()

    result = _run_git(["worktree", "list", "--porcelain"], cwd=workspace)
    if result.returncode != 0:
        logger.error(f"Failed to list worktrees: {result.stderr}")
        return []

    worktrees = []
    current_worktree: dict = {}

    for line in result.stdout.strip().split("\n"):
        if not line:
            if current_worktree:
                worktrees.append(current_worktree)
                current_worktree = {}
            continue

        if line.startswith("worktree "):
            current_worktree["path"] = line[9:]
        elif line.startswith("HEAD "):
            current_worktree["head"] = line[5:]
        elif line.startswith("branch "):
            current_worktree["branch"] = line[7:]
        elif line == "bare":
            current_worktree["bare"] = True
        elif line == "detached":
            current_worktree["detached"] = True
        elif line == "locked" or line.startswith("locked "):
            current_worktree["locked"] = line[7:] if len(line) > 6 else True

    if current_worktree:
        worktrees.append(current_worktree)

    return worktrees


def remove_worktree(
    worktree_path: Path,
    force: bool = False,
    workspace: Path | None = None,
) -> bool:
    """Remove a git worktree.

    Args:
        worktree_path: Path to the worktree to remove
        force: If True, force removal even if dirty
        workspace: Root workspace directory

    Returns:
        True if successful, False otherwise
    """
    if workspace is None:
        workspace = Path.cwd()

    args = ["worktree", "remove", str(worktree_path)]
    if force:
        args.append("--force")

    result = _run_git(args, cwd=workspace)

    if result.returncode != 0:
        logger.error(f"Failed to remove worktree: {result.stderr}")
        return False

    logger.info(f"Removed worktree at {worktree_path}")
    return True


def create_pr_from_worktree(
    worktree_path: Path,
    title: str,
    body: str | None = None,
    draft: bool = False,
    workspace: Path | None = None,
) -> str | None:
    """Create a GitHub PR from a worktree branch.

    Pushes the branch and creates a PR using gh CLI.

    Args:
        worktree_path: Path to the worktree
        title: PR title
        body: PR body (optional)
        draft: If True, create as draft PR
        workspace: Root workspace directory (for getting repo info)

    Returns:
        PR URL if successful, None otherwise
    """
    if workspace is None:
        workspace = Path.cwd()

    # Get the branch name
    result = _run_git(["branch", "--show-current"], cwd=worktree_path)
    if result.returncode != 0:
        logger.error(f"Failed to get current branch: {result.stderr}")
        return None

    branch = result.stdout.strip()

    # Push branch to origin
    result = _run_git(["push", "-u", "origin", branch], cwd=worktree_path)
    if result.returncode != 0:
        logger.error(f"Failed to push branch: {result.stderr}")
        return None

    # Create PR using gh CLI
    cmd = ["gh", "pr", "create", "--title", title]
    if body:
        cmd.extend(["--body", body])
    if draft:
        cmd.append("--draft")

    result = subprocess.run(cmd, cwd=worktree_path, capture_output=True, text=True)

    if result.returncode != 0:
        logger.error(f"Failed to create PR: {result.stderr}")
        return None

    # gh pr create outputs the PR URL
    pr_url = result.stdout.strip()
    logger.info(f"Created PR: {pr_url}")
    return pr_url


def merge_worktree(
    worktree_path: Path,
    target_branch: str = "master",
    workspace: Path | None = None,
    delete_after_merge: bool = True,
) -> bool:
    """Merge a worktree branch into target branch (for local swarm work).

    This is used when work should be merged locally rather than via PR.

    Args:
        worktree_path: Path to the worktree
        target_branch: Branch to merge into (default: master)
        workspace: Root workspace directory
        delete_after_merge: If True, remove worktree after merge

    Returns:
        True if successful, False otherwise
    """
    if workspace is None:
        workspace = Path.cwd()

    # Get the worktree branch name
    result = _run_git(["branch", "--show-current"], cwd=worktree_path)
    if result.returncode != 0:
        logger.error(f"Failed to get current branch: {result.stderr}")
        return False

    branch = result.stdout.strip()

    # Check for uncommitted changes
    result = _run_git(["status", "--porcelain"], cwd=worktree_path)
    if result.stdout.strip():
        logger.error("Worktree has uncommitted changes. Commit or stash first.")
        return False

    # Switch to main workspace and merge
    result = _run_git(["checkout", target_branch], cwd=workspace)
    if result.returncode != 0:
        logger.error(f"Failed to checkout {target_branch}: {result.stderr}")
        return False

    result = _run_git(["merge", branch, "--no-ff", "-m", f"Merge branch '{branch}'"], cwd=workspace)
    if result.returncode != 0:
        logger.error(f"Merge failed (conflict?): {result.stderr}")
        # Abort the merge
        _run_git(["merge", "--abort"], cwd=workspace)
        return False

    logger.info(f"Merged {branch} into {target_branch}")

    # Optionally remove the worktree
    if delete_after_merge:
        remove_worktree(worktree_path, workspace=workspace)
        # Also delete the branch
        _run_git(["branch", "-d", branch], cwd=workspace)

    return True


# FIX #3 & #4: Add base_branch parameter to get_worktree_status()
def get_worktree_status(worktree_path: Path, base_branch: str = "origin/master") -> dict:
    """Get status of a worktree (changes, commits ahead, etc).

    Args:
        worktree_path: Path to the worktree
        base_branch: Base branch to compare against (default: origin/master)

    Returns:
        Dict with status information
    """
    status: dict[str, object] = {
        "path": str(worktree_path),
        "branch": None,
        "commits_ahead": 0,
        "uncommitted_changes": False,
        "files_changed": [],
    }

    # Get branch name
    result = _run_git(["branch", "--show-current"], cwd=worktree_path)
    if result.returncode == 0:
        status["branch"] = result.stdout.strip()

    # Check for uncommitted changes
    result = _run_git(["status", "--porcelain"], cwd=worktree_path)
    if result.returncode == 0 and result.stdout.strip():
        status["uncommitted_changes"] = True
        status["files_changed"] = [line[3:] for line in result.stdout.strip().split("\n") if line]

    # Count commits ahead of base_branch (FIX: use base_branch parameter instead of hard-coded origin/master)
    result = _run_git(["rev-list", "--count", f"{base_branch}..HEAD"], cwd=worktree_path)
    if result.returncode == 0:
        try:
            status["commits_ahead"] = int(result.stdout.strip())
        except ValueError:
            pass

    return status


# A worktree is only cleaned up after this many days without a tracked file
# changing. Ancestry alone is not evidence that nobody is working in it.
DEFAULT_CLEANUP_IDLE_DAYS = 7.0


def _merged_branch_names(workspace: Path, base_branch: str) -> set[str] | None:
    """Return branch names merged into ``base_branch``, or None if unknown."""
    result = _run_git(["branch", "--merged", base_branch], cwd=workspace)
    if result.returncode != 0:
        return None

    # `git branch` marks the current branch with "* " and a branch checked out
    # in a linked worktree with "+ ". Both prefixes must be stripped or every
    # gptodo worktree looks unmerged.
    merged: set[str] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith(("* ", "+ ")):
            line = line[2:]
        if line:
            merged.add(line)
    return merged


def _newest_tracked_mtime(worktree_path: Path) -> float | None:
    """Return the newest mtime among a worktree's tracked files.

    ``None`` means the worktree could not be inspected, which callers must
    treat as "possibly active" rather than "idle".
    """
    result = _run_git(["ls-files", "-z"], cwd=worktree_path)
    if result.returncode != 0:
        return None

    newest: float | None = None
    for name in result.stdout.split("\0"):
        if not name:
            continue
        try:
            mtime = (worktree_path / name).stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    return newest


def _worktree_is_clean(worktree_path: Path) -> bool:
    """True when the worktree has no modified, staged or untracked files."""
    result = _run_git(["status", "--porcelain"], cwd=worktree_path)
    return result.returncode == 0 and not result.stdout.strip()


def cleanup_merged_worktrees(
    workspace: Path | None = None,
    base_branch: str = "origin/master",
    idle_days: float = DEFAULT_CLEANUP_IDLE_DAYS,
) -> int:
    """Remove worktrees that are merged *and* provably inactive.

    A branch being reachable from ``base_branch`` is not evidence that the
    worktree is unused: a worktree freshly created from the base branch points
    at an ancestor of it, so ``git branch --merged`` reports it as merged while
    an agent may be actively working in it (that regression deleted a live
    worktree from gptme/gptme-contrib#2474). Cleanup therefore requires all of:

    * the branch is merged into ``base_branch``;
    * the worktree has no uncommitted, staged or untracked files;
    * the worktree is not locked with ``git worktree lock``;
    * no tracked file has changed within ``idle_days``.

    Args:
        workspace: Root workspace directory
        base_branch: Base branch to check merge status against (default: origin/master)
        idle_days: Required quiet period in days for tracked files

    Returns:
        Count of worktrees cleaned up
    """
    if workspace is None:
        workspace = Path.cwd()

    worktrees_dir = get_worktrees_dir(workspace)
    if not worktrees_dir.exists():
        return 0

    merged_branches = _merged_branch_names(workspace, base_branch)
    if merged_branches is None:
        logger.error("Failed to list merged branches; skipping cleanup")
        return 0

    idle_cutoff = time.time() - idle_days * 86400
    count = 0

    for worktree in list_worktrees(workspace):
        path = Path(worktree.get("path", ""))
        branch = worktree.get("branch", "")

        # Skip if not in our worktrees directory
        if not str(path).startswith(str(worktrees_dir)):
            continue

        # Skip if no branch (detached HEAD)
        if not branch:
            continue

        # Extract just the branch name from refs/heads/...
        if branch.startswith("refs/heads/"):
            branch = branch[11:]

        if branch not in merged_branches:
            continue

        if worktree.get("locked"):
            logger.info(f"Skipping locked worktree: {path} (branch: {branch})")
            continue

        if not _worktree_is_clean(path):
            logger.info(f"Skipping dirty worktree: {path} (branch: {branch})")
            continue

        newest_mtime = _newest_tracked_mtime(path)
        if newest_mtime is None:
            logger.info(f"Skipping unreadable worktree: {path} (branch: {branch})")
            continue
        if newest_mtime > idle_cutoff:
            logger.info(f"Skipping recently active worktree: {path} (branch: {branch})")
            continue

        logger.info(f"Removing merged and idle worktree: {path} (branch: {branch})")
        if remove_worktree(path, workspace=workspace):
            # Also delete the branch
            _run_git(["branch", "-d", branch], cwd=workspace)
            count += 1

    return count
