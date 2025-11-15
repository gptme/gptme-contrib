#!/usr/bin/env python3
"""
Git-based lesson synchronization for agent network protocol.

Enables agents to share lessons via GitHub repository:
- Export local lessons to agent-specific directory
- Push changes to network
- Pull updates from other agents
- List available lessons from network

Part of Phase 4.3 Phase 2: Agent Network Protocol implementation.
"""

import argparse
import importlib
import subprocess
import sys
import sys as _sys
from pathlib import Path
from typing import Optional

try:
    from export import export_all_lessons  # type: ignore[import-not-found]

    # Use importlib for 'import' keyword conflict
    import_module = importlib.import_module("import")
    review_network_lesson = import_module.review_network_lesson
except ImportError as e:
    print(f"Error: Phase 1 modules (export.py, import.py) required: {e}")
    print("Install or add to PYTHONPATH")
    _sys.exit(1)


# Configuration
NETWORK_REPO = "gptme/gptme-lessons"
NETWORK_REPO_URL = f"https://github.com/{NETWORK_REPO}.git"
DEFAULT_NETWORK_DIR = Path.home() / ".gptme" / "network"


class SyncError(Exception):
    """Raised when sync operations fail."""

    pass


def run_git(
    args: list[str], cwd: Path, check: bool = True
) -> subprocess.CompletedProcess:
    """Run git command with error handling."""
    cmd = ["git"] + args
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, check=check
        )
        return result
    except subprocess.CalledProcessError as e:
        raise SyncError(f"Git command failed: {e.stderr}") from e


def init_repo(network_dir: Path = DEFAULT_NETWORK_DIR, force: bool = False) -> Path:
    """
    Initialize network repository (clone if needed).

    Args:
        network_dir: Local directory for network repo
        force: Force re-clone if already exists

    Returns:
        Path to network repository

    Raises:
        SyncError: If initialization fails
    """
    if network_dir.exists() and not force:
        # Verify it's a valid git repo
        try:
            run_git(["rev-parse", "--git-dir"], network_dir)
            print(f"✓ Network repo exists: {network_dir}")
            return network_dir
        except SyncError:
            print(f"⚠ Invalid repo at {network_dir}, re-cloning...")

    # Clone repository
    network_dir.parent.mkdir(parents=True, exist_ok=True)
    if network_dir.exists():
        import shutil

        shutil.rmtree(network_dir)

    print(f"📥 Cloning {NETWORK_REPO}...")
    try:
        subprocess.run(
            ["git", "clone", NETWORK_REPO_URL, str(network_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
        print(f"✓ Cloned to {network_dir}")
        return network_dir
    except subprocess.CalledProcessError as e:
        raise SyncError(f"Failed to clone: {e.stderr}") from e


def export_to_network(
    agent: str, lessons_dir: Path, network_dir: Path = DEFAULT_NETWORK_DIR
) -> int:
    """
    Export local lessons to agent-specific directory in network repo.

    Args:
        agent: Agent name (e.g., "bob")
        lessons_dir: Source lessons directory
        network_dir: Network repository directory

    Returns:
        Number of lessons exported

    Raises:
        SyncError: If export fails
    """
    agent_dir = network_dir / agent
    agent_dir.mkdir(parents=True, exist_ok=True)

    print(f"📤 Exporting lessons from {lessons_dir}...")
    print(f"   Target: {agent_dir}")

    # Use Phase 1 export function
    result = export_all_lessons(
        lessons_dir=lessons_dir, output_dir=agent_dir, agent_origin=agent, force=True
    )
    success_count: int = result.get("success", 0)
    failed_count: int = result.get("failed", 0)

    if failed_count > 0:
        print(f"⚠ {failed_count} lessons failed to export")

    print(f"✓ Exported {success_count} lessons to {agent_dir}")
    return success_count


def push_lessons(
    network_dir: Path = DEFAULT_NETWORK_DIR, message: Optional[str] = None
) -> bool:
    """
    Commit and push changes to network repository.

    Args:
        network_dir: Network repository directory
        message: Commit message (auto-generated if None)

    Returns:
        True if push succeeded, False if no changes

    Raises:
        SyncError: If push fails
    """
    # Check for changes
    status = run_git(["status", "--porcelain"], network_dir)
    if not status.stdout.strip():
        print("ℹ No changes to push")
        return False

    # Stage all changes
    run_git(["add", "."], network_dir)

    # Commit
    if message is None:
        message = "chore: sync lessons from autonomous agent"

    run_git(["commit", "-m", message], network_dir)

    # Push
    print("📤 Pushing to network...")
    run_git(["push"], network_dir)

    print("✓ Pushed successfully")
    return True


def pull_lessons(network_dir: Path = DEFAULT_NETWORK_DIR) -> bool:
    """
    Pull latest changes from network repository.

    Args:
        network_dir: Network repository directory

    Returns:
        True if updates were pulled, False if already up-to-date

    Raises:
        SyncError: If pull fails
    """
    print("📥 Pulling updates from network...")

    # Get current HEAD
    before = run_git(["rev-parse", "HEAD"], network_dir).stdout.strip()

    # Pull changes
    result = run_git(["pull"], network_dir, check=False)

    # Check if merge conflicts
    if result.returncode != 0:
        if "CONFLICT" in result.stdout:
            raise SyncError("Merge conflicts detected. Resolve manually.")
        raise SyncError(f"Pull failed: {result.stderr}")

    # Get new HEAD
    after = run_git(["rev-parse", "HEAD"], network_dir).stdout.strip()

    if before == after:
        print("ℹ Already up-to-date")
        return False

    print("✓ Pulled latest changes")
    return True


def list_network_lessons(
    network_dir: Path = DEFAULT_NETWORK_DIR, exclude_agent: Optional[str] = None
) -> dict[str, list[Path]]:
    """
    List available lessons from other agents in network.

    Args:
        network_dir: Network repository directory
        exclude_agent: Agent to exclude (typically current agent)

    Returns:
        Dict mapping agent names to lists of lesson paths
    """
    lessons_by_agent: dict[str, list[Path]] = {}

    for agent_dir in network_dir.iterdir():
        if not agent_dir.is_dir():
            continue
        if agent_dir.name.startswith("."):
            continue
        if exclude_agent and agent_dir.name == exclude_agent:
            continue

        # Find all lesson files
        lessons = list(agent_dir.rglob("*.md"))
        if lessons:
            lessons_by_agent[agent_dir.name] = lessons

    return lessons_by_agent


def sync(
    agent: str,
    lessons_dir: Path,
    network_dir: Path = DEFAULT_NETWORK_DIR,
    push_only: bool = False,
    pull_only: bool = False,
) -> tuple[int, int]:
    """
    Full sync workflow: pull, export, push.

    Args:
        agent: Agent name
        lessons_dir: Source lessons directory
        network_dir: Network repository directory
        push_only: Only push changes (no pull)
        pull_only: Only pull changes (no export/push)

    Returns:
        Tuple of (exported_count, pulled_updates)

    Raises:
        SyncError: If sync fails
    """
    # Initialize if needed
    init_repo(network_dir)

    exported = 0
    pulled = False

    # Pull updates (unless push-only)
    if not push_only:
        pulled = pull_lessons(network_dir)

    # Export and push (unless pull-only)
    if not pull_only:
        exported = export_to_network(agent, lessons_dir, network_dir)
        push_lessons(network_dir)

    return exported, pulled


def main():
    parser = argparse.ArgumentParser(
        description="Git-based lesson synchronization for agent network",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full sync (pull, export, push)
  %(prog)s --agent bob --lessons lessons/

  # Only push local changes
  %(prog)s --agent bob --lessons lessons/ --push-only

  # Only pull network updates
  %(prog)s --agent bob --pull-only

  # List available lessons from network
  %(prog)s --agent bob --list

  # Initialize/reset network repo
  %(prog)s --init
        """,
    )

    parser.add_argument(
        "--agent",
        default="bob",
        help="Agent name for directory structure (default: bob)",
    )
    parser.add_argument(
        "--lessons",
        type=Path,
        default=Path("lessons"),
        help="Local lessons directory (default: lessons/)",
    )
    parser.add_argument(
        "--network-dir",
        type=Path,
        default=DEFAULT_NETWORK_DIR,
        help=f"Network repository directory (default: {DEFAULT_NETWORK_DIR})",
    )

    # Actions
    parser.add_argument(
        "--init",
        action="store_true",
        help="Initialize network repository (clone if needed)",
    )
    parser.add_argument(
        "--list", action="store_true", help="List available lessons from other agents"
    )
    parser.add_argument(
        "--push-only", action="store_true", help="Only push changes (no pull)"
    )
    parser.add_argument(
        "--pull-only", action="store_true", help="Only pull changes (no export/push)"
    )
    parser.add_argument(
        "--force", action="store_true", help="Force re-clone network repo"
    )

    args = parser.parse_args()

    try:
        # Init action
        if args.init:
            init_repo(args.network_dir, force=args.force)
            return 0

        # List action
        if args.list:
            init_repo(args.network_dir)
            lessons = list_network_lessons(args.network_dir, exclude_agent=args.agent)

            if not lessons:
                print("ℹ No lessons available from other agents")
                return 0

            print("\n📚 Available lessons from network:\n")
            for agent, lesson_list in sorted(lessons.items()):
                print(f"  {agent}/ ({len(lesson_list)} lessons)")
                for lesson in sorted(lesson_list)[:5]:  # Show first 5
                    rel_path = lesson.relative_to(args.network_dir / agent)
                    print(f"    - {rel_path}")
                if len(lesson_list) > 5:
                    print(f"    ... and {len(lesson_list) - 5} more")
            return 0

        # Sync action (default)
        exported, pulled = sync(
            agent=args.agent,
            lessons_dir=args.lessons,
            network_dir=args.network_dir,
            push_only=args.push_only,
            pull_only=args.pull_only,
        )

        print("\n✓ Sync complete")
        print(f"  Exported: {exported} lessons")
        print(f"  Network updates: {'Yes' if pulled else 'No'}")

        return 0

    except SyncError as e:
        print(f"❌ Sync failed: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n⚠ Interrupted by user", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
