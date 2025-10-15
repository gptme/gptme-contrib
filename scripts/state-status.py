#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "click>=8.0.0",
#     "rich>=13.0.0",
#     "python-frontmatter>=1.1.0",
# ]
# [tool.uv]
# exclude-newer = "2024-04-01T00:00:00Z"
# ///
"""Show git status-like view of state directories.

Features:
- Shows status of files in state directories (tasks/, tweets/, etc)
- Reports issues like missing/multiple links
- Provides summary statistics
- Similar formatting to git status
"""

import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import (
    Dict,
    List,
    Optional,
    Tuple,
)

import click
import frontmatter
from rich.console import Console
from rich.table import Table

# Configure logging
logger = logging.getLogger(__name__)


@dataclass
class DirectoryConfig:
    """Configuration for a directory type."""

    type_name: str
    states: list[str]
    special_files: list[str]
    emoji: str  # Emoji for visual distinction


CONFIGS = {
    "tasks": DirectoryConfig(
        type_name="tasks",
        states=["new", "active", "paused", "done", "cancelled"],
        special_files=["README.md"],
        emoji="📋",
    ),
    "tweets": DirectoryConfig(
        type_name="tweets",
        states=["new", "queued", "approved", "posted"],
        special_files=["README.md", "templates"],
        emoji="🐦",
    ),
    "email": DirectoryConfig(
        type_name="email",
        states=["inbox", "drafts", "sent", "archive"],
        special_files=["README.md", "templates", "config"],
        emoji="📧",
    ),
}


def find_repo_root(start_path: Path) -> Path:
    """Find the repository root by looking for .git directory."""
    current = start_path.resolve()
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    return start_path.resolve()


@dataclass
class FileStatus:
    """Status of a file in a state directory."""

    path: Path
    state: Optional[str]  # Current state from frontmatter
    issues: List[str]  # Any issues with the file
    created: datetime
    modified: datetime


class StateChecker:
    """Check state directories for issues and status."""

    def __init__(self, repo_root: Path, config: DirectoryConfig):
        self.root = repo_root
        self.config = config
        self.base_dir = repo_root / config.type_name

    def check_file(self, file: Path) -> FileStatus:
        """Check status and issues for a single file."""
        issues = []

        # Read frontmatter
        post = frontmatter.load(file)
        metadata = post.metadata

        # Check state
        current_state = metadata.get("state")
        if not current_state:
            issues.append("No state in frontmatter")
            current_state = "new"  # Default state
        elif current_state not in self.config.states:
            issues.append(f"Invalid state: {current_state}")

        # Get timestamps from frontmatter or git
        try:
            created = datetime.fromisoformat(metadata.get("created", ""))
            modified = datetime.fromisoformat(metadata.get("modified", ""))
        except (ValueError, TypeError):
            # Fallback to git timestamps
            try:
                # Get last commit time
                result = subprocess.run(
                    ["git", "log", "-1", "--format=%at", "--", str(file)],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                timestamp = int(result.stdout.strip())
                modified = datetime.fromtimestamp(timestamp)

                # Get first commit time (creation)
                result = subprocess.run(
                    ["git", "log", "--reverse", "--format=%at", "--", str(file)],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                timestamp = int(result.stdout.strip().split("\n")[0])
                created = datetime.fromtimestamp(timestamp)
            except (subprocess.CalledProcessError, ValueError, IndexError):
                # Fallback to filesystem timestamps if git fails
                stats = file.stat()
                created = datetime.fromtimestamp(stats.st_ctime)
                modified = datetime.fromtimestamp(stats.st_mtime)

        return FileStatus(
            path=file,
            state=current_state,
            issues=issues,
            created=created,
            modified=modified,
        )

    def check_all(self) -> Dict[str, List[FileStatus]]:
        """Check all files and categorize by state."""
        results: Dict[str, List[FileStatus]] = {
            "untracked": [],  # Files with no state
            "issues": [],  # Files with problems
        }
        # Initialize state lists
        for state in self.config.states:
            results[state] = []

        # Check each file in base directory only
        for file in self.base_dir.glob("*.md"):
            # Skip special files
            if file.name in self.config.special_files:
                continue

            status = self.check_file(file)

            # Categorize based on status
            if status.issues:
                results["issues"].append(status)
            elif not status.state:
                results["untracked"].append(status)
            else:
                results[status.state].append(status)

        return results


def format_time_ago(dt: datetime) -> str:
    """Format a datetime as a human-readable time ago string."""
    now = datetime.now()
    delta = now - dt

    if delta < timedelta(minutes=1):
        return "just now"
    elif delta < timedelta(hours=1):
        minutes = int(delta.total_seconds() / 60)
        return f"{minutes}m ago"
    elif delta < timedelta(days=1):
        hours = int(delta.total_seconds() / 3600)
        return f"{hours}h ago"
    elif delta < timedelta(days=30):
        days = delta.days
        return f"{days}d ago"
    else:
        return dt.strftime("%Y-%m-%d")


# State-specific styling
STATE_STYLES = {
    # Tasks
    "new": ("yellow", "new"),
    "active": ("blue", "active"),
    "paused": ("cyan", "paused"),
    "done": ("green", "done"),
    "cancelled": ("red", "cancelled"),
    # Tweets
    "queued": ("yellow", "queued"),
    "approved": ("blue", "approved"),
    "posted": ("green", "posted"),
    # Email
    "inbox": ("yellow", "inbox"),
    "drafts": ("blue", "draft"),
    "sent": ("green", "sent"),
    "archive": ("cyan", "archived"),
    # Special categories
    "issues": ("red", "!"),
    "untracked": ("dim", "?"),
}


def print_status_section(console: Console, title: str, items: List[FileStatus], show_state: bool = False):
    """Print a section of the status output."""
    if not items:
        return

    # Get style for this section
    style, emoji = STATE_STYLES.get(
        title.split()[-1].lower(),  # Get last word of title
        ("white", "•"),  # Default style
    )

    # Sort items by modified time
    sorted_items = sorted(items, key=lambda x: x.modified, reverse=True)

    # Limit new tasks to 5, show count of remaining
    if title.lower().endswith("new"):
        if len(sorted_items) > 5:
            display_items = sorted_items[:5]
            remaining = len(sorted_items) - 5
        else:
            display_items = sorted_items
            remaining = 0
    else:
        display_items = sorted_items
        remaining = 0

    # Print header with count
    state_name = title.split()[-1].upper()
    console.print(f"\n{state_name} ({len(items)}):")

    # Print items
    for item in display_items:
        name = os.path.splitext(os.path.basename(item.path))[0]  # Remove .md and path
        modified = format_time_ago(item.modified)

        if show_state and item.state:
            state_style, state_text = STATE_STYLES.get(item.state, ("white", "•"))
            console.print(f"  {name} ({state_text}, {modified})")
        else:
            console.print(f"  {name} ({modified})")

        # Show issues inline
        if item.issues:
            console.print(f"    ! {', '.join(item.issues)}")

    # Show remaining count for new tasks
    if remaining > 0:
        console.print(f"  ... and {remaining} more")


def print_summary(console: Console, results: Dict[str, List[FileStatus]], config: DirectoryConfig):
    """Print summary statistics."""
    total = 0
    state_summary = []

    # Get counts by state
    for state in config.states:
        count = len(results.get(state, []))
        if count > 0:
            total += count
            state_text = STATE_STYLES.get(state, ("white", state))[1]
            state_summary.append(f"{count} {state_text}")

    # Add special categories
    if results.get("untracked"):
        count = len(results["untracked"])
        total += count
        state_summary.append(f"{count} untracked")
    if results.get("issues"):
        count = len(results["issues"])
        total += count
        state_summary.append(f"{count} issues")

    # Print compact summary
    if state_summary:
        console.print(f"\n{config.emoji} Summary: {total} total ({', '.join(state_summary)})")


def check_directory(console: Console, dir_type: str, repo_root: Path) -> None:
    """Check and display status for a single directory type."""
    config = CONFIGS[dir_type]
    checker = StateChecker(repo_root, config)
    results = checker.check_all()

    # Print header with type-specific color
    style, _ = STATE_STYLES.get(config.states[0], ("white", "•"))
    console.print(f"\n[bold {style}]{config.emoji} {config.type_name.title()} Status[/]\n")

    # Print sections
    if results["issues"]:
        print_status_section(
            console,
            "Issues Found",
            results["issues"],
            show_state=True,
        )

    if results["untracked"]:
        print_status_section(
            console,
            "Untracked Files",
            results["untracked"],
        )

    # Print active states first
    active_states = ["new", "active", "queued", "inbox", "drafts"]
    for state in active_states:
        if state in config.states and results.get(state):
            print_status_section(
                console,
                f"In {state}",
                results[state],
            )

    # Print summary
    print_summary(console, results, config)


def print_total_summary(console: Console, all_results: Dict[str, Dict[str, List[FileStatus]]]):
    """Print summary of all directory types."""
    table = Table(title="\n📊 Total Summary", show_header=False, title_style="bold")
    table.add_column("Category", style="bold")
    table.add_column("Count", justify="right")
    table.add_column("Details", justify="left")

    total_items = 0
    total_issues = 0
    # Type annotation for rows: (category, count, details)
    rows: List[Tuple[str, int, str]] = []

    for dir_type, results in all_results.items():
        config = CONFIGS[dir_type]
        type_total = sum(len(items) for items in results.values())
        type_issues = len(results.get("issues", []))
        total_items += type_total
        total_issues += type_issues

        # Get counts by state
        state_counts = []
        for state in config.states:
            count = len(results.get(state, []))
            if count > 0:
                style, emoji = STATE_STYLES.get(state, ("white", "•"))
                state_counts.append(f"[{style}]{count} {emoji}[/]")

        if state_counts:
            rows.append(
                (
                    config.emoji + " " + config.type_name,
                    type_total,
                    " ".join(state_counts),
                )
            )

    # Add rows to table
    for category, count, details in rows:
        table.add_row(category, str(count), details)  # Convert count to string

    # Add total row
    table.add_row("", "", "")  # Empty row as separator
    table.add_row(
        "[bold]Total[/]",
        f"{total_items}",  # Format as string
        f"[yellow]{total_issues} issues[/]" if total_issues else "",
    )

    console.print(table)


@click.command()
@click.option(
    "--type",
    "dir_type",
    type=click.Choice(list(CONFIGS.keys())),
    default="tasks",
    help="Type of directory to check",
)
@click.option(
    "--all",
    "check_all",
    is_flag=True,
    default=False,
    help="Check all directory types",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    default=False,
    help="Show verbose output",
)
def main(dir_type: str = "tasks", check_all: bool = False, verbose: bool = False) -> None:
    """Show git status-like view of state directories."""
    # Configure logging
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s: %(message)s",
    )

    # Initialize
    repo_root = find_repo_root(Path.cwd())
    console = Console()

    if check_all:
        # Check all directory types and collect results
        all_results = {}
        for i, type_name in enumerate(CONFIGS.keys()):
            if i > 0:
                # Add separator between types
                console.print("\n" + "─" * 50 + "\n")

            # Get results for this type
            config = CONFIGS[type_name]
            checker = StateChecker(repo_root, config)
            results = checker.check_all()
            all_results[type_name] = results

            # Print individual type status
            check_directory(console, type_name, repo_root)

        # Print total summary at the end
        console.print("\n" + "─" * 50 + "\n")
        print_total_summary(console, all_results)
    else:
        # Check single directory type (default to tasks)
        dir_type = dir_type or "tasks"
        check_directory(console, dir_type, repo_root)


if __name__ == "__main__":
    main()
