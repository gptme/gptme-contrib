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

import importlib
import subprocess
import sys
import sys as _sys
from pathlib import Path

try:
    from gptme_lessons_extras.export import export_all_lessons

    # Use importlib for 'import' keyword conflict
    import_module = importlib.import_module("lessons.import")
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
    # is_symlink() matters too: a dangling link does not exist() but renaming
    # the clone over it would silently discard the link without --force.
    if (network_dir.exists() or network_dir.is_symlink()) and not force:
        if not network_dir.is_dir():
            raise SyncError(
                f"{network_dir} exists but is not a directory. "
                "Move it aside; --force replaces it."
            )
        # It must be the root of its own work tree: a directory inside an
        # enclosing repo (e.g. a versioned home) would make us push that repo.
        top = run_git(["rev-parse", "--show-toplevel"], network_dir, check=False)
        if top.returncode == 0 and network_dir.samefile(top.stdout.rstrip("\n")):
            print(f"✓ Network repo exists: {network_dir}")
            return network_dir
        # Never delete a directory we did not create without --force. Git can
        # also refuse a valid clone (e.g. "dubious ownership"), so show why and
        # do not steer the user straight to --force.
        if top.returncode == 0:
            reason = f"it is inside the work tree {top.stdout.strip()}"
        else:
            reason = top.stderr.strip()
        raise SyncError(
            f"{network_dir} exists but cannot be used as the network repo: "
            f"{reason}\nFix the problem or move the directory aside. "
            "--force deletes it, including any unpushed commits."
        )

    # Clone next to the target first, so a failed clone (offline, auth)
    # leaves any existing directory untouched.
    network_dir.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    import tempfile

    try:
        staging = Path(
            tempfile.mkdtemp(prefix=f".{network_dir.name}.", dir=network_dir.parent)
        )
    except OSError as e:
        raise SyncError(f"Failed to create a staging directory: {e}") from e
    clone_dir = staging / network_dir.name
    # Only clear the staging directory when the old directory is not parked in
    # it: the cleanup must never be able to delete the user's data.
    keep_staging = False
    print(f"📥 Cloning {NETWORK_REPO}...")
    try:
        subprocess.run(
            ["git", "clone", NETWORK_REPO_URL, str(clone_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
        # Move the old directory into staging instead of deleting it in place,
        # so a failed swap can put it back. A symlink is moved as a link: its
        # target is left alone.
        old_dir = staging / "old"
        replace = force and (network_dir.exists() or network_dir.is_symlink())
        if replace:
            # Set the guard before the rename: staging holds the user's only
            # copy from that point on, and a signal delivered after the rename
            # syscall completes but before the next statement must not let the
            # cleanup delete it.
            keep_staging = True
            try:
                network_dir.rename(old_dir)
            except OSError:
                keep_staging = False
                raise
        try:
            clone_dir.rename(network_dir)
        except OSError as swap_error:
            if replace:
                try:
                    old_dir.rename(network_dir)
                except OSError as restore_error:
                    # Keep the original directory: report where it is instead
                    # of letting the staging cleanup delete it.
                    raise SyncError(
                        f"Failed to move the clone into {network_dir} "
                        f"({swap_error}) and could not restore the original "
                        f"directory; it is kept at {old_dir} ({restore_error})."
                    ) from restore_error
                keep_staging = False
            raise
        keep_staging = False
        print(f"✓ Cloned to {network_dir}")
        return network_dir
    except subprocess.CalledProcessError as e:
        raise SyncError(f"Failed to clone: {e.stderr}") from e
    except OSError as e:
        raise SyncError(f"Failed to replace {network_dir}: {e}") from e
    finally:
        parked = staging / "old"
        if keep_staging and parked.exists():
            # The original directory is parked here and the target path no
            # longer holds it; say where, or the user cannot find it back.
            print(
                f"⚠ The original directory is kept at {parked}; "
                "move it back if the new clone is not usable."
            )
        else:
            shutil.rmtree(staging, ignore_errors=True)
            if staging.exists():
                print(
                    f"⚠ Could not fully remove the staging directory {staging}; "
                    "it may still hold a copy of the replaced repository."
                )


def export_to_network(
    agent: str, lessons_dir: Path, network_dir: Path = DEFAULT_NETWORK_DIR
) -> int:
    """
    Export local lessons to agent-specific directory in network repo.

    Args:
        agent: Agent name (e.g., "agent")
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
    network_dir: Path = DEFAULT_NETWORK_DIR, message: str | None = None
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
        # A commit from an earlier sync whose push failed leaves the tree
        # clean, so also check for commits the upstream does not have yet.
        ahead = run_git(["rev-list", "--count", "@{u}..HEAD"], network_dir, check=False)
        if ahead.returncode == 0 and ahead.stdout.strip() not in ("", "0"):
            print("📤 Pushing commits from an earlier failed push...")
            run_git(["push"], network_dir)
            print("✓ Pushed successfully")
            return True
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
    network_dir: Path = DEFAULT_NETWORK_DIR, exclude_agent: str | None = None
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
    import click

    @click.command(
        epilog="""
Examples:

  # Full sync (pull, export, push)

  sync --agent <agent> --lessons lessons/

  # Only push local changes

  sync --agent <agent> --lessons lessons/ --push-only

  # Only pull network updates

  sync --agent <agent> --pull-only

  # List available lessons from network

  sync --agent <agent> --list

  # Initialize/reset network repo

  sync --init
        """,
    )
    @click.option(
        "--agent",
        default="agent",
        help="Agent name for directory structure (default: agent)",
    )
    @click.option(
        "--lessons",
        type=click.Path(),
        default="lessons",
        help="Local lessons directory (default: lessons/)",
    )
    @click.option(
        "--network-dir",
        type=click.Path(),
        default=str(DEFAULT_NETWORK_DIR),
        help=f"Network repository directory (default: {DEFAULT_NETWORK_DIR})",
    )
    @click.option(
        "--init",
        "do_init",
        is_flag=True,
        help="Initialize network repository (clone if needed)",
    )
    @click.option(
        "--list",
        "do_list",
        is_flag=True,
        help="List available lessons from other agents",
    )
    @click.option("--push-only", is_flag=True, help="Only push changes (no pull)")
    @click.option(
        "--pull-only", is_flag=True, help="Only pull changes (no export/push)"
    )
    @click.option("--force", is_flag=True, help="Force re-clone network repo")
    def cli(agent, lessons, network_dir, do_init, do_list, push_only, pull_only, force):
        """Git-based lesson synchronization for agent network."""
        lessons_path = Path(lessons)
        network_path = Path(network_dir)

        try:
            # Init action
            if do_init:
                init_repo(network_path, force=force)
                return

            # List action
            if do_list:
                init_repo(network_path)
                found_lessons = list_network_lessons(network_path, exclude_agent=agent)

                if not found_lessons:
                    print("ℹ No lessons available from other agents")
                    return

                print("\n📚 Available lessons from network:\n")
                for agent_name, lesson_list in sorted(found_lessons.items()):
                    print(f"  {agent_name}/ ({len(lesson_list)} lessons)")
                    for lesson in sorted(lesson_list)[:5]:  # Show first 5
                        rel_path = lesson.relative_to(network_path / agent_name)
                        print(f"    - {rel_path}")
                    if len(lesson_list) > 5:
                        print(f"    ... and {len(lesson_list) - 5} more")
                return

            # Sync action (default)
            exported, pulled = sync(
                agent=agent,
                lessons_dir=lessons_path,
                network_dir=network_path,
                push_only=push_only,
                pull_only=pull_only,
            )

            print("\n✓ Sync complete")
            print(f"  Exported: {exported} lessons")
            print(f"  Network updates: {'Yes' if pulled else 'No'}")

        except SyncError as e:
            print(f"❌ Sync failed: {e}", file=sys.stderr)
            sys.exit(1)
        except KeyboardInterrupt:
            print("\n⚠ Interrupted by user", file=sys.stderr)
            sys.exit(130)

    cli()


if __name__ == "__main__":
    main()
