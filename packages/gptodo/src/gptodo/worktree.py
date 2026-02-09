"""Git worktree management for isolated agent execution.

Provides worktree creation, cleanup, and PR/merge workflows for agent isolation.
This enables multiple agents to work on different features simultaneously
without conflicts.

Implements Issue #246: worktree workflow for isolated agent execution.
"""

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Literal

logger = logging.getLogger(__name__)


@dataclass
class WorktreeInfo:
    """Information about a git worktree."""

    path: Path
    branch: str
    task_id: str
    base_branch: str = "origin/master"
    status: Literal["active", "merged", "pr_created", "removed"] = "active"


def get_worktrees_dir(workspace: Optional[Path] = None) -> Path:
    """Get the worktrees directory, creating if needed.

    Worktrees are stored in .worktrees/ by default to keep them
    separate from regular project files.
    """
    if workspace is None:
        workspace = Path.cwd()
    worktrees_dir = workspace / ".worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    return worktrees_dir


def _run_git(args: list[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    """Run a git command and return the result."""
    cmd = ["git"] + args
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def create_worktree(
    task_id: str,
    branch_name: Optional[str] = None,
    base_branch: str = "origin/master",
    workspace: Optional[Path] = None,
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
    fetch_ref = base_branch.split("/", 1)[1] if "/" in base_branch else base_branch
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

    # Unset upstream to prevent accidental push to base branch
    _run_git(["branch", "--unset-upstream"], cwd=worktree_path)

    logger.info(f"Created worktree at {worktree_path} on branch {branch_name}")

    return WorktreeInfo(
        path=worktree_path,
        branch=branch_name,
        task_id=task_id,
        base_branch=base_branch,
        status="active",
    )


def list_worktrees(workspace: Optional[Path] = None) -> list[dict]:
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

    if current_worktree:
        worktrees.append(current_worktree)

    return worktrees


def remove_worktree(
    worktree_path: Path,
    force: bool = False,
    workspace: Optional[Path] = None,
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
    body: Optional[str] = None,
    draft: bool = False,
    workspace: Optional[Path] = None,
) -> Optional[str]:
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
    workspace: Optional[Path] = None,
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


def get_worktree_status(worktree_path: Path) -> dict:
    """Get status of a worktree (changes, commits ahead, etc).

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

    # Count commits ahead of origin/master
    result = _run_git(["rev-list", "--count", "origin/master..HEAD"], cwd=worktree_path)
    if result.returncode == 0:
        try:
            status["commits_ahead"] = int(result.stdout.strip())
        except ValueError:
            pass

    return status


def cleanup_merged_worktrees(workspace: Optional[Path] = None) -> int:
    """Remove worktrees whose branches have been merged.

    Returns:
        Count of worktrees cleaned up
    """
    if workspace is None:
        workspace = Path.cwd()

    worktrees_dir = get_worktrees_dir(workspace)
    if not worktrees_dir.exists():
        return 0

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

        # Check if branch is merged into origin/master
        result = _run_git(["branch", "--merged", "origin/master"], cwd=workspace)
        # Use exact line matching to avoid substring false positives
        # (e.g. "task-foo" matching "task-foobar")
        merged_branches = [b.strip().lstrip("* ") for b in result.stdout.splitlines()]
        if result.returncode == 0 and branch in merged_branches:
            logger.info(f"Removing merged worktree: {path} (branch: {branch})")
            if remove_worktree(path, workspace=workspace):
                # Also delete the branch
                _run_git(["branch", "-d", branch], cwd=workspace)
                count += 1

    return count
