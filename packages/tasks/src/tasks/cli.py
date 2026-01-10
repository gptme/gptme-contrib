#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "click>=8.0.0",
#     "rich>=13.0.0",
#     "python-frontmatter>=1.1.0",
#     "tabulate>=0.9.0",
# ]
# [tool.uv]
# exclude-newer = "2024-04-01T00:00:00Z"
# ///

"""Task verification and status CLI for gptme agents.

Features:
- Status views
- Task metadata verification
- Dependency validation
- Link checking
"""

import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    NamedTuple,
    Optional,
    Set,
    Tuple,
    Union,
)

import click
import frontmatter
from rich.console import Console
from rich.table import Table
from tabulate import tabulate


# Trajectory integration code (inline to avoid import issues in uv scripts)


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
        states=["new", "active", "paused", "done", "cancelled", "someday"],
        special_files=["README.md", "templates", "video-scripts"],
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


class SubtaskCount(NamedTuple):
    """Count of completed and total subtasks."""

    completed: int
    total: int

    def __str__(self) -> str:
        """Return string representation like (4/16)."""
        return f"({self.completed}/{self.total})" if self.total > 0 else ""


@dataclass
class TaskInfo:
    """Information about a task with metadata and validation.

    This class represents a task file with its metadata, content analysis,
    and validation status. It provides a unified interface for accessing
    task information across the codebase.

    Attributes:
        path: Path to the task file
        name: Filename without .md extension
        state: Current state from frontmatter (new, active, paused, etc.)
        created: Creation timestamp
        modified: Last modification timestamp
        priority: Task priority (high, medium, low)
        tags: List of tags
        depends: List of task dependencies
        subtasks: Count of completed and total subtasks
        issues: List of validation issues
        metadata: Raw frontmatter metadata
    """

    path: Path
    name: str
    state: Optional[str]
    created: datetime
    modified: datetime
    priority: Optional[str]
    tags: List[str]
    depends: List[str]
    subtasks: SubtaskCount
    issues: List[str]
    metadata: Dict

    @property
    def id(self) -> str:
        """Get task ID (filename without .md)."""
        return self.name

    @property
    def created_ago(self) -> str:
        """Get human-readable time since creation."""
        return format_time_ago(self.created)

    @property
    def modified_ago(self) -> str:
        """Get human-readable time since last modification."""
        return format_time_ago(self.modified)

    @property
    def has_issues(self) -> bool:
        """Check if task has any validation issues."""
        return len(self.issues) > 0

    @property
    def priority_rank(self) -> int:
        """Get numeric priority rank for sorting.

        Returns:
            int: Priority rank (3=high, 2=medium, 1=low, 0=none)
        """
        # Handle None case explicitly to satisfy type checker
        if self.priority is None:
            return PRIORITY_RANK[None]
        return PRIORITY_RANK.get(self.priority, 0)

    def __str__(self) -> str:
        """Return a human-readable string representation."""
        status = []
        if self.state:
            status.append(self.state)
        if self.priority:
            status.append(self.priority)
        if self.subtasks.total > 0:
            status.append(f"{self.subtasks.completed}/{self.subtasks.total}")

        status_str = f" ({', '.join(status)})" if status else ""
        return f"{self.name}{status_str}"


def count_subtasks(content: str) -> SubtaskCount:
    """Count completed and total subtasks in markdown content.

    Looks for markdown task list items in the format:
    - [ ] Incomplete task
    - [x] Completed task
    - ✅ Completed task
    - 🏃 In-progress task
    - [SKIP] Skipped task (not counted)

    Returns:
        SubtaskCount with completed and total counts
    """
    completed = len(re.findall(r"- (\[x\]|✅)", content))
    total = len(re.findall(r"- (\[ \]|🏃)", content)) + completed
    return SubtaskCount(completed, total)


def validate_task_file(file: Path, post: frontmatter.Post) -> List[str]:
    """Validate a task file's format and required fields.

    Args:
        file: Path to the task file
        post: Loaded frontmatter post

    Returns:
        List of validation issues
    """
    issues = []
    metadata = post.metadata

    # Check required fields
    required_fields: Dict[str, type | tuple[type, ...]] = {
        "state": str,
        "created": (str, datetime),  # Can be string or datetime
    }

    for field, expected_type in required_fields.items():
        if field not in metadata:
            issues.append(f"Missing required field: {field}")
        elif isinstance(expected_type, tuple):
            if not isinstance(metadata[field], expected_type):
                type_names = " or ".join(t.__name__ for t in expected_type)
                issues.append(f"Field {field} must be {type_names}")
        elif not isinstance(metadata[field], expected_type):
            issues.append(f"Field {field} must be {expected_type.__name__}")

    # Validate state value
    if "state" in metadata:
        state = metadata["state"]
        if state not in CONFIGS["tasks"].states:
            issues.append(f"Invalid state: {state}")

    # Validate created date format if string (accepts date-only or full datetime)
    if "created" in metadata and isinstance(metadata["created"], str):
        try:
            datetime.fromisoformat(metadata["created"])
        except ValueError:
            # Try parsing as date-only
            try:
                from datetime import date

                date.fromisoformat(metadata["created"])
            except ValueError:
                issues.append(
                    "Created date must be ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)"
                )

    # Optional field validation
    if "priority" in metadata:
        priority = metadata["priority"]
        if priority not in ("high", "medium", "low", None):
            issues.append("Priority must be 'high', 'medium', or 'low'")

    if "tags" in metadata and not isinstance(metadata["tags"], list):
        issues.append("Tags must be a list")

    if "depends" in metadata and not isinstance(metadata["depends"], list):
        issues.append("Dependencies must be a list")

    return issues


def load_tasks(
    tasks_dir: Path, recursive: bool = False, single_file: Optional[Path] = None
) -> List[TaskInfo]:
    """Load tasks from directory or single file with metadata.

    Args:
        tasks_dir: Directory containing task files
        recursive: Whether to search subdirectories
        single_file: Optional specific file to load

    Returns:
        List of TaskInfo objects
    """
    tasks = []

    # Directories to exclude
    excluded_dirs = {"templates", "video-scripts", "agent-setup-interview"}

    # Handle single file case
    if single_file:
        if not single_file.exists():
            logging.error(f"File not found: {single_file}")
            return []
        files = [single_file]
    else:
        # Determine glob pattern based on recursive flag
        pattern = "**/*.md" if recursive else "*.md"
        files = [
            f
            for f in tasks_dir.glob(pattern)
            if not recursive or not any(d in f.parts for d in excluded_dirs)
        ]

    for file in files:
        try:
            # Read frontmatter and content
            post = frontmatter.load(file)
            metadata = post.metadata

            # Validate file format and required fields
            issues = validate_task_file(file, post)

            # Count subtasks
            subtasks = count_subtasks(post.content)

            # Get state (default to new if missing)
            state = metadata.get("state")
            if not state:
                issues.append("No state in frontmatter")
                state = "new"  # Default state

            # Parse timestamps
            # Helper to parse datetime fields (accepts date-only or full datetime)
            def parse_datetime_field(value) -> datetime:
                """Parse datetime field that could be date-only or full datetime."""
                if isinstance(value, datetime):
                    return value
                value_str = str(value)
                try:
                    return datetime.fromisoformat(value_str)
                except ValueError:
                    # Try parsing as date-only
                    from datetime import date

                    date_obj = date.fromisoformat(value_str)
                    return datetime.combine(date_obj, datetime.min.time())

            try:
                created = parse_datetime_field(metadata.get("created", ""))
                modified = parse_datetime_field(metadata.get("modified", ""))
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

            # Convert to naive datetime if timezone-aware
            if created.tzinfo:
                created = created.astimezone().replace(tzinfo=None)
            if modified.tzinfo:
                modified = modified.astimezone().replace(tzinfo=None)

            # Create TaskInfo object
            task = TaskInfo(
                path=file,
                name=file.stem,
                state=state,
                created=created,
                modified=modified,
                priority=metadata.get("priority"),
                tags=metadata.get("tags", []),
                depends=metadata.get("depends", []),
                subtasks=subtasks,
                issues=issues,
                metadata=metadata,
            )
            tasks.append(task)

        except Exception as e:
            logging.error(f"Error reading {file}: {e}")

    return tasks


def find_repo_root(start_path: Path) -> Path:
    """Find the repository root by looking for .git directory."""
    current = start_path.resolve()
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    return start_path.resolve()


def format_time_ago(dt: datetime) -> str:
    """Format a datetime as a human-readable time ago string."""
    # Convert to naive datetime if timezone-aware
    if dt.tzinfo:
        dt = dt.astimezone().replace(tzinfo=None)
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

# State emojis for consistent use
STATE_EMOJIS = {
    "new": "🆕",
    "active": "🏃",
    "paused": "⚪",
    "done": "✅",
    "cancelled": "❌",
    "issues": "⚠️",
    "untracked": "❓",
    # priorities
    "high": "🔴",
    "medium": "🟡",
    "low": "🟢",
}


@click.group()
@click.option("-v", "--verbose", is_flag=True)
def cli(verbose):
    """Task verification and status CLI."""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=log_level)


def load_task(file: Path) -> Tuple[frontmatter.Post, SubtaskCount]:
    """Load a task file and count its subtasks."""
    post = frontmatter.load(file)
    subtasks = count_subtasks(post.content)
    return post, subtasks


@cli.command("show")
@click.argument("task_id", required=False)
def show_(task_id):
    """Show detailed information about a task.

    If task_id is not provided, it will show the first task found.
    """
    show(task_id)


def show(task_id):
    """Show detailed information about a task."""
    console = Console()
    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    if not task_id:
        console.print("[red]Error: Task ID or filename required[/]")
        return

    # Load all tasks
    tasks = load_tasks(tasks_dir)
    if not tasks:
        console.print("[red]No tasks found[/]")
        return

    # Sort tasks by creation date for consistent ID mapping
    tasks.sort(key=lambda t: t.created)

    # Find requested task
    task = None
    if task_id.isdigit():
        # Get task by numeric ID
        idx = int(task_id) - 1
        if 0 <= idx < len(tasks):
            task = tasks[idx]
    else:
        # Get task by name
        task_name = task_id[:-3] if task_id.endswith(".md") else task_id
        matching = [t for t in tasks if t.name == task_name]
        if matching:
            task = matching[0]

    if not task:
        console.print(f"[red]Error: Task {task_id} not found[/]")
        return

    # Create rich table for metadata
    table = Table(show_header=False, box=None)
    table.add_column("Key", style="bold")
    table.add_column("Value")

    # Add metadata rows
    table.add_row("File", str(task.path.relative_to(repo_root)))
    table.add_row("State", task.state or "unknown")
    table.add_row("Created", task.created_ago)
    table.add_row("Modified", task.modified_ago)
    if task.priority:
        table.add_row("Priority", STATE_EMOJIS.get(task.priority) or task.priority)
    if task.tags:
        table.add_row("Tags", ", ".join(task.tags))
    if task.depends:
        table.add_row("Dependencies", ", ".join(task.depends))
    if task.subtasks.total > 0:
        table.add_row(
            "Subtasks", f"{task.subtasks.completed}/{task.subtasks.total} completed"
        )
    if task.issues:
        table.add_row("Issues", ", ".join(task.issues))

    # Print metadata table
    console.print("\n[bold]Task Metadata:[/]")
    console.print(table)

    # Print content
    console.print("\n[bold]Content:[/]")
    post = frontmatter.load(task.path)  # Reload to get content
    console.out(post.content, highlight=True)


@cli.command("list")
@click.option(
    "--sort",
    type=click.Choice(["state", "date", "name", "completion"]),
    default="date",
    help="Sort by state, creation date, name, or completion percentage",
)
@click.option(
    "--active-only",
    is_flag=True,
    help="Only show new and active tasks",
)
@click.option(
    "--context",
    type=str,
    default=None,
    help="Filter by context tag (e.g., @coding, @research)",
)
def list_(sort, active_only, context):
    """List all tasks in a table format."""
    console = Console()
    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    # Load all tasks
    all_tasks = load_tasks(tasks_dir)
    if not all_tasks:
        console.print("[red]No tasks found[/]")
        return

    # Create stable enumerated ID mapping based on creation date for ALL tasks
    tasks_by_date = sorted(all_tasks, key=lambda t: t.created)
    name_to_enum_id = {task.name: i for i, task in enumerate(tasks_by_date, 1)}
    # Keep a mapping of all task names for dependency resolution
    all_tasks_dict = {task.name: task for task in all_tasks}

    # Filter tasks if active-only flag is set
    tasks = all_tasks
    if active_only:
        tasks = [task for task in all_tasks if task.state in ["new", "active"]]
        if not tasks:
            console.print("[yellow]No new or active tasks found[/]")
            return
        console.print("[blue]Showing only new and active tasks[/]\n")

    # Filter by context if specified
    if context:
        # Normalize context tag (add @ if missing)
        context_tag = context if context.startswith("@") else f"@{context}"
        tasks = [task for task in tasks if context_tag in (task.tags or [])]
        if not tasks:
            console.print(f"[yellow]No tasks found with context tag '{context_tag}'[/]")
            return
        console.print(f"[blue]Showing tasks with context tag '{context_tag}'[/]\n")

    # Sort tasks for display based on option
    if sort == "state":
        tasks.sort(key=lambda t: (t.state or "", t.created))
    elif sort == "name":
        tasks.sort(key=lambda t: t.name)
    elif sort == "completion":
        # Calculate completion percentage, grouping tasks with no subtasks at the bottom
        def completion_key(t):
            if t.subtasks.total == 0:
                return (0, t.created)  # Group at bottom, sort by date within group
            completion_pct = t.subtasks.completed / t.subtasks.total
            return (
                1,
                completion_pct,
                t.created,
            )  # Sort by completion %, newest first within same %

        # Sort in reverse order to get:
        # 1. Tasks with subtasks first (1 > 0)
        # 2. Higher completion percentages first
        # 3. Newer tasks first within same percentage
        tasks.sort(key=completion_key, reverse=True)
    else:  # default: date
        tasks.sort(key=lambda t: t.created)

    # Create display rows
    display_rows = []
    for task in tasks:
        # Get stable enumerated ID for task
        enum_id = name_to_enum_id[task.name]

        # Calculate completion info
        if task.subtasks.total > 0:
            completion_pct = (task.subtasks.completed / task.subtasks.total) * 100
            completion_str = f"{completion_pct:>3.0f}%"
            name_with_count = f"{task.name} {task.subtasks}"
        else:
            completion_str = "  -"
            name_with_count = task.name

        # Format dependencies with enumerated IDs or task info
        if task.depends:
            dep_ids = []
            for dep in task.depends:
                if dep in all_tasks_dict:
                    dep_task = all_tasks_dict[dep]
                    # If dependency is in filtered list, show its ID
                    if not active_only or dep_task.state in ["new", "active"]:
                        dep_ids.append(str(name_to_enum_id[dep]))
                    else:
                        # Show task name and state for filtered out dependencies
                        state_emoji = STATE_EMOJIS.get(
                            dep_task.state or "untracked", "•"
                        )
                        dep_ids.append(f"{dep} ({state_emoji})")
                else:
                    dep_ids.append(f"{dep} (missing)")
            deps_str = ", ".join(dep_ids)
        else:
            deps_str = ""

        # Add row with state emoji
        state_emoji = STATE_EMOJIS.get(task.state or "untracked", "•")
        display_rows.append(
            [
                state_emoji,
                f"{enum_id}. {name_with_count}",
                task.created_ago,
                STATE_EMOJIS.get(task.priority or "") or task.priority or "",
                completion_str,
                deps_str,
            ]
        )

    # Print table
    headers = ["", "Task", "Created", "Priority", "Complete", "Deps"]
    # Only show dependencies column if any task has dependencies
    has_deps = any(task.depends for task in tasks)
    if not has_deps:
        display_rows = [row[:-1] for row in display_rows]
        headers = headers[:-1]

    # Set column alignments and widths
    colaligns = ["left", "left", "left", "center"]
    colwidths = [2, None, None, None]
    if has_deps:
        colaligns.append("left")
        colwidths.append(20)

    console.print(
        "\n"
        + tabulate(
            display_rows,
            headers=headers,
            tablefmt="simple",
            maxcolwidths=colwidths,
            colalign=colaligns,
        )
    )

    # Print legend for tasks with dependencies
    if has_deps:
        tasks_with_deps = [
            (task, name_to_enum_id[task.name]) for task in tasks if task.depends
        ]
        if tasks_with_deps:
            console.print("\nDependencies:")
            for task, enum_id in tasks_with_deps:
                dep_strs = []
                for dep in task.depends:
                    if dep in all_tasks_dict:
                        dep_task = all_tasks_dict[dep]
                        # If dependency is in filtered list, show its ID
                        if not active_only or dep_task.state in ["new", "active"]:
                            dep_strs.append(f"{dep} ({name_to_enum_id[dep]})")
                        else:
                            # Show task name and state for filtered out dependencies
                            state_emoji = STATE_EMOJIS.get(
                                dep_task.state or "untracked", "•"
                            )
                            dep_strs.append(f"{dep} ({state_emoji})")
                    else:
                        dep_strs.append(f"{dep} (missing)")
                dep_str = ", ".join(dep_strs)
                console.print(f"  {task.name} ({enum_id}) -> {dep_str}")

    # Print summary
    state_counts: Dict[str, int] = {}
    for task in tasks:
        emoji = STATE_EMOJIS.get(task.state or "untracked", "•")
        state_counts[emoji] = state_counts.get(emoji, 0) + 1

    summary = [f"{count} {state}" for state, count in state_counts.items()]
    console.print(f"\nTotal: {len(tasks)} tasks ({', '.join(summary)})")


class StateChecker:
    """Check state directories for issues and status."""

    def __init__(self, repo_root: Path, config: DirectoryConfig):
        self.root = repo_root
        self.config = config
        self.base_dir = repo_root / config.type_name

    def check_all(self) -> Dict[str, List[TaskInfo]]:
        """Check all files and categorize by state."""
        results: Dict[str, List[TaskInfo]] = {
            "untracked": [],  # Files with no state
            "issues": [],  # Files with problems
        }
        # Initialize state lists
        for state in self.config.states:
            results[state] = []

        # Load all tasks from base directory
        tasks = load_tasks(self.base_dir)

        # Categorize tasks based on state and issues
        for task in tasks:
            # Skip special files
            if task.path.name in self.config.special_files:
                continue

            # Categorize based on status
            if task.issues:
                results["issues"].append(task)
            elif not task.state:
                results["untracked"].append(task)
            else:
                results[task.state].append(task)

        return results


def print_status_section(
    console: Console, title: str, items: List[TaskInfo], show_state: bool = False
):
    """Print a section of the status output."""
    if not items:
        return

    # Sort items by creation date (newest first)
    items = sorted(items, key=lambda x: x.created, reverse=True)

    # Get style for this section
    state_name = title.split()[-1].lower()
    style, emoji = STATE_STYLES.get(state_name, ("white", "•"))

    # Limit new tasks to 5, show count of remaining
    if state_name == "new":
        if len(items) > 5:
            display_items = items[:5]
            remaining = len(items) - 5
        else:
            display_items = items
            remaining = 0
    else:
        display_items = items
        remaining = 0

    # Print header with count and emoji
    emoji = STATE_EMOJIS.get(state_name, "•")
    console.print(f"\n{emoji} {title.upper()} ({len(items)}):")

    # Print items
    for task in display_items:
        # Format display string
        subtask_str = f" {task.subtasks}" if task.subtasks.total > 0 else ""
        priority_str = f" [{task.priority}]" if task.priority else ""

        # Get state info if needed
        state_info = ""
        if show_state:
            # Use "untracked" for None state, with fallback to default style
            state = task.state or "untracked"
            _, state_text = STATE_STYLES.get(state, ("white", "•"))
            state_info = f", {state_text}"

        # Print task info
        console.print(
            f"  {task.name}{subtask_str}{priority_str} ({task.created_ago}{state_info})"
        )

        # Show issues inline
        if task.issues:
            console.print(f"    ! {', '.join(task.issues)}")

    # Show remaining count for new tasks
    if remaining > 0:
        console.print(f"  ... and {remaining} more")


def print_summary(
    console: Console, results: Dict[str, List[TaskInfo]], config: DirectoryConfig
):
    """Print summary statistics."""
    total = 0
    state_counts: Dict[str, int] = {}

    # Count tasks by state
    for state, items in results.items():
        count = len(items)
        if count > 0:
            total += count
            state_counts[state] = count

    # Build summary strings
    summary_parts = []

    # Add regular states first
    for state in config.states:
        if count := state_counts.get(state, 0):
            style, state_text = STATE_STYLES.get(state, ("white", state))
            emoji = STATE_EMOJIS.get(state, "•")
            summary_parts.append(f"{count} {emoji}")

    # Add special categories
    if count := state_counts.get("untracked", 0):
        summary_parts.append(f"{count} ❓")
    if count := state_counts.get("issues", 0):
        summary_parts.append(f"{count} ⚠️")

    # Print compact summary
    if summary_parts:
        console.print(
            f"\n{config.emoji} Summary: {total} total ({', '.join(summary_parts)})"
        )


def check_directory(
    console: Console, dir_type: str, repo_root: Path, compact: bool = False
) -> Dict[str, List[TaskInfo]]:
    """Check and display status for a single directory type."""
    config = CONFIGS[dir_type]
    checker = StateChecker(repo_root, config)
    results = checker.check_all()

    # Print header with type-specific color
    style, _ = STATE_STYLES.get(config.states[0], ("white", "•"))
    console.print(
        f"\n[bold {style}]{config.emoji} {config.type_name.title()} Status[/]\n"
    )

    # Print sections in order
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

    # Determine which states to show based on compact mode
    states_to_show = ["new", "active"] if compact else config.states

    # Print active states in order
    for state in states_to_show:
        if state in config.states and results.get(state):
            print_status_section(
                console,
                state,
                results[state],
            )

    # Print summary
    print_summary(console, results, config)

    return results


def print_total_summary(
    console: Console, all_results: Dict[str, Dict[str, List[TaskInfo]]]
):
    """Print summary of all directory types."""
    table = Table(title="\n📊 Total Summary", show_header=False, title_style="bold")
    table.add_column("Category", style="bold")
    table.add_column("Count", justify="right")
    table.add_column("Details", justify="left")

    total_items = 0
    total_issues = 0

    # Process each directory type
    for dir_type, results in all_results.items():
        config = CONFIGS[dir_type]

        # Calculate totals
        type_total = sum(len(items) for items in results.values())
        type_issues = len(results.get("issues", []))
        total_items += type_total
        total_issues += type_issues

        if type_total == 0:
            continue

        # Build state summary
        state_summary = []
        for state in config.states:
            if count := len(results.get(state, [])):
                emoji = STATE_EMOJIS.get(state, "•")
                state_summary.append(f"{count} {emoji}")

        # Add special categories
        if count := len(results.get("untracked", [])):
            state_summary.append(f"{count} ❓")
        if type_issues:
            state_summary.append(f"{type_issues} ⚠️")

        # Add row to table
        table.add_row(
            config.emoji + " " + config.type_name,
            str(type_total),
            " ".join(state_summary),
        )

    # Add separator and total row
    if total_items > 0:
        table.add_row("", "", "")  # Empty row as separator
        table.add_row(
            "[bold]Total[/]",
            str(total_items),
            f"[yellow]{total_issues} issues[/]" if total_issues else "",
        )

        console.print(table)


@cli.command()
@click.option("--type", type=click.Choice(list(CONFIGS.keys())), default="tasks")
@click.option("--all", is_flag=True, help="Check all directory types")
@click.option("--compact", is_flag=True, help="Only show new and active tasks")
@click.option("--summary", is_flag=True, help="Only show summary")
@click.option("--issues", is_flag=True, help="Only show items with issues")
def status(type, all, compact, summary, issues):
    """Show status of tasks and other tracked items."""
    console = Console()
    repo_root = find_repo_root(Path.cwd())

    # Collect results from all directories
    all_results = {}

    if all:
        # Check all directory types
        for type_name in CONFIGS.keys():
            results = check_directory(console, type_name, repo_root, compact)
            if results:  # Only include directories with items
                all_results[type_name] = results

            # Add separator between types if not last
            if type_name != list(CONFIGS.keys())[-1]:
                console.print("\n" + "─" * 50)

        # Print total summary at the end
        if len(all_results) > 1:
            console.print("\n" + "─" * 50)
            print_total_summary(console, all_results)

    else:
        # Check single directory type
        results = check_directory(console, type, repo_root, compact)
        if results:
            all_results[type] = results

    # Additional filtering based on options
    if issues:
        # Show only items with issues across all types
        has_issues = False
        for dir_type, results in all_results.items():
            if issue_items := results.get("issues", []):
                has_issues = True
                config = CONFIGS[dir_type]
                console.print(f"\n{config.emoji} {dir_type.title()} Issues:")
                for item in issue_items:
                    console.print(f"  • {item.name}: {', '.join(item.issues)}")

        if not has_issues:
            console.print("\n[green]No issues found![/]")

    elif summary:
        # Show only the summary for each type
        for dir_type, results in all_results.items():
            config = CONFIGS[dir_type]
            print_summary(console, results, config)


@cli.command()
@click.option("--fix", is_flag=True, help="Try to fix simple issues")
@click.argument("task_files", nargs=-1, type=click.Path())
def check(fix: bool, task_files: list[str]):
    """Check task integrity and relationships.

    If task files are provided, only check those files.
    Otherwise, check all tasks in the tasks directory.
    """
    console = Console()

    # Find repo root and tasks directory
    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    # ALWAYS load all tasks to build complete task_ids set for dependency checking
    all_tasks = load_tasks(tasks_dir)
    if not all_tasks:
        console.print("[yellow]No tasks found in tasks directory![/]")
        return

    # Build complete task_ids set from ALL tasks
    task_ids = {task.id for task in all_tasks}

    # Determine which tasks to validate
    if task_files:
        # Only validate the specified files
        tasks_to_validate = []
        for file in task_files:
            path = Path(file)
            # Handle different path formats from pre-commit
            if path.is_absolute():
                # Already absolute, use as-is
                pass
            elif str(path).startswith("tasks/"):
                # Path includes tasks/ prefix, resolve from repo root
                path = repo_root / path
            else:
                # Just filename, resolve from tasks_dir
                path = tasks_dir / path
            try:
                file_tasks = load_tasks(path.parent, single_file=path)
                if file_tasks:
                    tasks_to_validate.extend(file_tasks)
                else:
                    console.print(f"[yellow]Warning: No valid task found in {file}[/]")
            except Exception as e:
                console.print(f"[red]Error reading {file}: {e}[/]")
        if not tasks_to_validate:
            console.print("[yellow]No valid tasks to validate![/]")
            return
    else:
        # Validate all tasks
        tasks_to_validate = all_tasks

    # Track dependencies in tasks being validated
    tasks_with_deps = [task for task in tasks_to_validate if task.depends]

    def has_cycle(task_id: str, visited: Set[str], path: Set[str]) -> bool:
        """Check for circular dependencies."""
        if task_id in path:
            return True
        if task_id in visited:
            return False
        visited.add(task_id)
        path.add(task_id)
        # Find task object to get its dependencies (search in ALL tasks)
        task = next((t for t in all_tasks if t.id == task_id), None)
        if task:
            for dep in task.depends:
                if has_cycle(dep, visited, path):
                    return True
        path.remove(task_id)
        return False

    # Group issues by type
    validation_issues: list[str] = []
    dependency_issues: list[str] = []
    cycle_issues: list[str] = []

    # Collect validation issues from tasks being validated
    for task in tasks_to_validate:
        if task.issues:
            validation_issues.extend(f"{task.id}: {issue}" for issue in task.issues)

    # Check for missing dependencies
    for task in tasks_with_deps:
        for dep in task.depends:
            if dep not in task_ids:
                dependency_issues.append(f"{task.id}: Dependency '{dep}' not found")

    # Check for circular dependencies
    for task in tasks_with_deps:
        if has_cycle(task.id, set(), set()):
            cycle_issues.append(
                f"Circular dependency detected involving task {task.id}"
            )

    # TODO: Implement link checking
    # for task in tasks:
    #     check_links(task)

    # Report results by category
    has_issues = False

    if validation_issues:
        has_issues = True
        console.print("\n[bold red]Validation Issues:[/]")
        for issue in validation_issues:
            console.print(f"  • {issue}")

    if dependency_issues:
        has_issues = True
        console.print("\n[bold red]Dependency Issues:[/]")
        for issue in dependency_issues:
            console.print(f"  • {issue}")

    if cycle_issues:
        has_issues = True
        console.print("\n[bold red]Circular Dependencies:[/]")
        for issue in cycle_issues:
            console.print(f"  • {issue}")

    if has_issues:
        if fix:
            console.print("\n[yellow]Auto-fix not implemented yet[/]")
            console.print("Suggested fixes:")
            console.print("  • Add missing frontmatter fields")
            console.print("  • Fix invalid state values")
            console.print("  • Update or remove invalid dependencies")
            console.print("  • Break circular dependencies")
        sys.exit(1)
    else:
        total = len(tasks_to_validate)
        with_subtasks = sum(1 for t in tasks_to_validate if t.subtasks.total > 0)
        console.print(
            f"\n[bold green]✓ All {total} tasks verified successfully! "
            f"({with_subtasks} with subtasks)[/]"
        )


# Add priority ranking to the top of the file, after imports
PRIORITY_RANK: dict[str | None, int] = {
    "urgent": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
    None: 0,  # Tasks without priority
}


def task_to_dict(task: TaskInfo) -> Dict[str, Any]:
    """Serialize TaskInfo to a JSON-compatible dictionary.

    Returns dict with:
    - id: task filename without .md
    - state: current state
    - priority: task priority
    - created: ISO timestamp
    - modified: ISO timestamp
    - tags: list of tags
    - depends: list of dependencies
    - subtasks: {completed: int, total: int}
    """
    return {
        "id": task.id,
        "name": task.name,
        "state": task.state,
        "priority": task.priority,
        "created": task.created.isoformat() if task.created else None,
        "modified": task.modified.isoformat() if task.modified else None,
        "tags": task.tags,
        "depends": task.depends,
        "subtasks": {
            "completed": task.subtasks.completed,
            "total": task.subtasks.total,
        },
        "has_issues": task.has_issues,
    }


def is_task_ready(task: TaskInfo, all_tasks: Dict[str, TaskInfo]) -> bool:
    """Check if a task is ready (unblocked) to work on.

    A task is ready if:
    - It has no dependencies, OR
    - All its dependencies are in "done" or "cancelled" state

    Args:
        task: Task to check
        all_tasks: Dictionary mapping task names to TaskInfo objects

    Returns:
        True if task is ready, False if blocked
    """
    if not task.depends:
        # No dependencies = always ready
        return True

    # Check if all dependencies are completed
    for dep_name in task.depends:
        dep_task = all_tasks.get(dep_name)
        if dep_task is None:
            # Missing dependency = blocked (should be validated separately)
            return False
        if dep_task.state not in ["done", "cancelled"]:
            # Dependency not completed = blocked
            return False

    # All dependencies completed = ready
    return True


def resolve_tasks(
    task_ids: List[str], tasks: List[TaskInfo], tasks_dir: Path
) -> List[TaskInfo]:
    """Resolve tasks by ID/path, supporting both task names and paths.

    Args:
        task_ids: List of task identifiers (names or paths)
        tasks: List of all tasks
        tasks_dir: Path to tasks directory

    Returns:
        List of matched tasks
    """
    matched_tasks = []
    for task_id in task_ids:
        # Handle both task names and paths
        task_path = Path(task_id)
        if task_path.suffix == ".md":
            # Compute repo root from tasks dir
            repo_root = tasks_dir.parent
            # Try different path resolutions
            paths_to_try = [
                task_path,  # As-is
                tasks_dir / task_path,  # Relative to tasks dir
                tasks_dir / task_path.name,  # Just the filename
                repo_root / task_path,  # Relative to repo root
            ]
            # Try to find task by any of the paths
            task = None
            for path in paths_to_try:
                task = next((t for t in tasks if t.path == path.resolve()), None)
                if task:
                    break
        else:
            # Find task by name
            task = next((t for t in tasks if t.name == task_id), None)

        if not task:
            raise ValueError(f"Task not found: {task_id}")
        matched_tasks.append(task)

    return matched_tasks


@cli.command("edit")
@click.argument("task_ids", nargs=-1, required=True)
@click.option(
    "--set",
    "set_fields",
    type=(str, str),
    multiple=True,
    help="Set a field value (state, priority, created)",
)
@click.option(
    "--add",
    "add_fields",
    type=(str, str),
    multiple=True,
    help="Add value to a list field (depends, tags)",
)
@click.option(
    "--remove",
    "remove_fields",
    type=(str, str),
    multiple=True,
    help="Remove value from a list field (depends, tags)",
)
@click.option(
    "--set-subtask",
    "set_subtask",
    type=(str, str),
    help="Set subtask state (subtask_text, state). State must be 'done' or 'todo'",
)
def edit(task_ids, set_fields, add_fields, remove_fields, set_subtask):
    """Edit task metadata.

    Examples:
        tasks edit task-123 --set state active
        tasks edit task-123 --set priority high
        tasks edit task-123 --set created 2025-05-05T10:00:00+02:00
        tasks edit task-123 --add depends other-task
        tasks edit task-123 --add tag feature
        tasks edit task-123 --remove tag wip
        tasks edit task-123 --set state active --add tag feature --add depends other-task
        tasks edit task-123 --set-subtask "Handle simple responses" done

    Date formats:
        The created field accepts ISO format dates:
        - Date only: 2025-05-05
        - Date and time: 2025-05-05T10:00:00
        - With timezone: 2025-05-05T10:00:00+02:00
    """
    console = Console()
    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    # Load all tasks
    tasks = load_tasks(tasks_dir)
    if not tasks:
        console.print("[red]No tasks found[/]")
        return

    # Find tasks to edit
    try:
        target_tasks = resolve_tasks(task_ids, tasks, tasks_dir)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        return

    # Validate all field operations before applying any changes
    changes: list[tuple[str, str, str | None]] = []

    # Define valid fields and their validation rules
    VALID_FIELDS: dict[str, dict[str, object]] = {
        # Required fields
        "state": {"type": "enum", "values": CONFIGS["tasks"].states},
        "created": {"type": "date"},
        # Optional fields with validation
        "priority": {"type": "enum", "values": ["high", "medium", "low", "none"]},
        "task_type": {"type": "enum", "values": ["project", "action", "none"]},
        "assigned_to": {"type": "enum", "values": ["agent", "human", "both", "none"]},
        "waiting_since": {"type": "date"},
        # Optional fields with arbitrary string values
        "next_action": {"type": "string"},
        "waiting_for": {"type": "string"},
        # List fields handled separately via --add/--remove
        "tags": {"type": "list"},
        "depends": {"type": "list"},
        "output_types": {"type": "list"},
    }

    # Validate set operations
    for field, value in set_fields:
        # Check if field is valid
        if field not in VALID_FIELDS:
            console.print(
                f"[red]Unknown field: {field}. Valid fields: {', '.join(sorted(VALID_FIELDS.keys()))}[/]"
            )
            return

        field_spec = VALID_FIELDS[field]

        # Handle "none" special value to clear field
        if value == "none":
            changes.append(("set", field, None))
            continue

        # List fields should use --add/--remove instead
        if field_spec["type"] == "list":
            console.print(
                f"[red]Field '{field}' is a list field. Use --add or --remove instead of --set.[/]"
            )
            return

        # Validate based on field type
        if field_spec["type"] == "enum":
            if value not in field_spec["values"]:  # type: ignore[operator]
                valid = ", ".join(field_spec["values"])  # type: ignore[arg-type]
                console.print(
                    f"[red]Invalid {field}: {value}. Valid values: {valid}[/]"
                )
                return
        elif field_spec["type"] == "date":
            try:
                # Parse and validate the date format
                created_dt = datetime.fromisoformat(value)
                # Convert to string format for storage
                value = created_dt.isoformat()
            except ValueError:
                console.print(
                    f"[red]Invalid {field} date format. Use ISO format (YYYY-MM-DD[THH:MM:SS+HH:MM])[/]"
                )
                return
        elif field_spec["type"] == "string":
            # Arbitrary string value - no validation needed
            pass

        changes.append(("set", field, value))

    # Validate subtask operation
    if set_subtask:
        subtask_text, state = set_subtask
        if state not in ["done", "todo"]:
            console.print(
                f"[red]Invalid subtask state: {state}. Valid values: done, todo[/]"
            )
            return
        changes.append(("set_subtask", subtask_text, state))

    # Validate add/remove operations
    for op, fields in [("add", add_fields), ("remove", remove_fields)]:
        for field, value in fields:
            if field not in ("depends", "deps", "tags", "tag", "dep"):
                console.print(
                    f"[red]Cannot {op} to field: {field}. Use --{op} with deps/tags.[/]"
                )
                return

            # Normalize field names (tag -> tags, dep -> depends, deps -> depends)
            field_map = {"tag": "tags", "dep": "depends", "deps": "depends"}
            field = field_map.get(field, field)
            changes.append((op, field, value))

    if not changes:
        console.print("[red]No changes specified. Use --set, --add, or --remove.[/]")
        return

    # Show changes to be made
    console.print("\nChanges to apply:")
    for task in target_tasks:
        task_changes = []

        # Group changes by field for cleaner display
        field_changes: dict[str, list[tuple[str, str | None]]] = {}
        for op, field, value in changes:
            if field not in field_changes:
                field_changes[field] = []
            field_changes[field].append((op, value))

        # Show changes for each field
        for field, field_ops in field_changes.items():
            if field in ("deps", "tags"):
                current = task.metadata.get(field, [])
                new = current.copy()

                # Apply all operations for this field
                for op, value in field_ops:
                    if op == "add":
                        new = list(set(new + [value]))
                    else:  # remove
                        new = [x for x in new if x != value]

                if new != current:
                    task_changes.append(
                        f"{field}: {', '.join(current)} -> {', '.join(new)}"
                    )
            else:
                # For set operations, only show the final value
                set_ops = [v for op, v in field_ops if op == "set"]
                if set_ops:
                    current = task.metadata.get(field)
                    new = set_ops[-1]  # Use the last set value
                    if new != current:
                        task_changes.append(f"{field}: {current} -> {new}")

        if task_changes:
            console.print(f"  {task.name}:")
            for change in task_changes:
                console.print(f"    {change}")

    # Apply changes
    for task in target_tasks:
        post = frontmatter.load(task.path)

        # Apply all changes
        for op, field, value in changes:
            if op == "set_subtask":
                # field is subtask_text, value is state ("done" or "todo")
                subtask_text = field
                state = value

                # Parse markdown body to find and update subtask
                lines = post.content.split("\n")
                updated = False
                for i, line in enumerate(lines):
                    # Check if this line is a subtask checkbox with matching text
                    if subtask_text in line and ("- [ ]" in line or "- [x]" in line):
                        if state == "done":
                            lines[i] = line.replace("- [ ]", "- [x]")
                        else:  # state == "todo"
                            lines[i] = line.replace("- [x]", "- [ ]")
                        updated = True
                        break

                if not updated:
                    console.print(f"[red]Subtask not found: {subtask_text}[/]")
                    return

                post.content = "\n".join(lines)
            elif field in ("depends", "deps", "tags"):
                # Handle list fields (after normalization, "dep"/"deps" → "depends")
                current = post.metadata.get(field, [])
                if op == "add":
                    post.metadata[field] = list(set(current + [value]))
                else:  # remove
                    post.metadata[field] = [x for x in current if x != value]
            else:  # set operation
                if value is None:  # Clear field with "none" value
                    post.metadata.pop(field, None)
                else:
                    post.metadata[field] = value

        # Save changes
        with open(task.path, "w") as f:
            f.write(frontmatter.dumps(post))

    # Check if any tasks were marked as done and run completion hook
    state_changes = [
        (op, field, value) for op, field, value in changes if field == "state"
    ]
    if any(value == "done" for _, _, value in state_changes):
        for task in target_tasks:
            # Re-load task to get updated metadata
            post = frontmatter.load(task.path)
            if post.metadata.get("state") == "done":
                # Run task completion hook if configured via env var
                import os
                import subprocess

                hook_cmd = os.environ.get("HOOK_TASK_DONE")
                if hook_cmd:
                    try:
                        subprocess.run(
                            [hook_cmd, task.id, task.name, str(repo_root)], check=False
                        )
                    except Exception as e:
                        console.print(
                            f"[yellow]Note: Task completion hook error: {e}[/]"
                        )

    # Show success message
    count = len(target_tasks)
    console.print(f"[green]✓ Updated {count} task{'s' if count > 1 else ''}[/]")


@cli.command("tags")
@click.option("--state", help="Filter by task state")
@click.option("--list", "show_tasks", is_flag=True, help="List tasks for each tag")
@click.argument("filter_tags", nargs=-1)
def tags(state: Optional[str], show_tasks: bool, filter_tags: tuple[str, ...]):
    """List all tags and their task counts.

    Examples:
        tasks tags                    # Show all tags and counts
        tasks tags --list            # Show tags with task lists
        tasks tags --state active    # Only count active tasks
        tasks tags automation ai     # Show specific tags
    """
    console = Console()
    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    # Load all tasks
    tasks = load_tasks(tasks_dir)
    if not tasks:
        console.print("[yellow]No tasks found![/]")
        return

    # Filter by state if specified
    if state:
        if state not in CONFIGS["tasks"].states:
            console.print(f"[red]Invalid state: {state}[/]")
            return
        tasks = [t for t in tasks if t.state == state]
        if not tasks:
            console.print(f"[yellow]No tasks with state '{state}'[/]")
            return
        console.print(f"[blue]Showing tags for {state} tasks[/]\n")

    # Collect tags and count tasks
    tag_tasks: Dict[str, List[TaskInfo]] = {}
    for task in tasks:
        for tag in task.tags:
            if tag not in tag_tasks:
                tag_tasks[tag] = []
            tag_tasks[tag].append(task)

    if not tag_tasks:
        console.print("[yellow]No tags found![/]")
        return

    # Filter tags if specified
    if filter_tags:
        filtered_tags = {}
        for tag in filter_tags:
            if tag in tag_tasks:
                filtered_tags[tag] = tag_tasks[tag]
            else:
                console.print(f"[yellow]Warning: Tag '{tag}' not found[/]")
        tag_tasks = filtered_tags
        if not tag_tasks:
            console.print("[yellow]No matching tags found![/]")
            return

    # Sort tags by frequency (most used first)
    sorted_tags = sorted(tag_tasks.items(), key=lambda x: (-len(x[1]), x[0]))

    # Print header
    console.print("\n🏷️  Task Tags")

    # Create rows for tabulate
    rows = []
    for tag, tag_task_list in sorted_tags:
        count = len(tag_task_list)
        # Always show tasks if specific tags were requested
        if show_tasks or filter_tags:
            # Sort tasks by state and name
            tag_task_list.sort(key=lambda t: (t.state or "", t.name))
            # Format task list with state emojis
            task_list = []
            for task in tag_task_list:
                emoji = STATE_EMOJIS.get(task.state or "untracked", "•")
                task_list.append(f"{emoji} {task.name}")
            tasks_str = "\n".join(task_list)
            rows.append([tag, str(count), tasks_str])
        else:
            rows.append([tag, str(count)])

    # Print table using tabulate with simple format
    headers = (
        ["Tag", "Count", "Tasks"] if (show_tasks or filter_tags) else ["Tag", "Count"]
    )
    console.print(tabulate(rows, headers=headers, tablefmt="plain"))

    # Print summary
    total_tags = len(sorted_tags)
    total_tasks = len(tasks)
    tagged_tasks = len(set(task.name for tasks in tag_tasks.values() for task in tasks))
    console.print(
        f"\nFound {total_tags} tags across {tagged_tasks} tasks "
        f"({total_tasks - tagged_tasks} untagged)"
    )


@cli.command("ready")
@click.option(
    "--state",
    type=click.Choice(["new", "active", "both"]),
    default="both",
    help="Filter by task state (new, active, or both)",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help="Output as JSON for machine consumption",
)
def ready(state, output_json):
    """List all ready (unblocked) tasks.

    Shows tasks that have no dependencies or whose dependencies are all completed.
    Inspired by beads' `bd ready` command for finding work to do.

    Use --json for machine-readable output in autonomous workflows.
    """
    console = Console()
    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    # Load all tasks
    all_tasks = load_tasks(tasks_dir)
    if not all_tasks:
        console.print("[yellow]No tasks found![/]")
        return

    # Create task lookup dictionary
    tasks_dict = {task.name: task for task in all_tasks}

    # Filter by state first
    if state == "new":
        filtered_tasks = [task for task in all_tasks if task.state == "new"]
    elif state == "active":
        filtered_tasks = [task for task in all_tasks if task.state == "active"]
    else:  # both
        filtered_tasks = [task for task in all_tasks if task.state in ["new", "active"]]

    # Filter for ready (unblocked) tasks
    ready_tasks = [task for task in filtered_tasks if is_task_ready(task, tasks_dict)]

    if not ready_tasks:
        if output_json:
            print(json.dumps({"ready_tasks": [], "count": 0}, indent=2))
            return
        console.print("[yellow]No ready tasks found![/]")
        console.print(
            "\n[dim]Tip: Try checking blocked tasks with --state to see dependencies[/]"
        )
        return

    # Sort by priority (high to low) and then by creation date (oldest first)
    ready_tasks.sort(
        key=lambda t: (
            -t.priority_rank,
            t.created,
        )
    )

    # JSON output for machine consumption
    if output_json:
        result = {
            "ready_tasks": [task_to_dict(t) for t in ready_tasks],
            "count": len(ready_tasks),
        }
        print(json.dumps(result, indent=2))
        return

    # Create table
    table = Table(title=f"[bold green]Ready Tasks[/] ({len(ready_tasks)} unblocked)")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("State", style="blue")
    table.add_column("Priority", style="yellow")
    table.add_column("Task", style="white")
    table.add_column("Subtasks", style="magenta")

    # Create stable enumerated ID mapping
    tasks_by_date = sorted(all_tasks, key=lambda t: t.created)
    name_to_enum_id = {task.name: i for i, task in enumerate(tasks_by_date, 1)}

    for task in ready_tasks:
        enum_id = name_to_enum_id[task.name]
        state_emoji = STATE_EMOJIS.get(task.state or "untracked", "•")
        priority_emoji = STATE_EMOJIS.get(task.priority or "", "")

        if task.subtasks.total > 0:
            subtasks_str = f"{task.subtasks.completed}/{task.subtasks.total}"
        else:
            subtasks_str = "-"

        table.add_row(
            str(enum_id),
            state_emoji,
            priority_emoji or (task.priority or ""),
            task.name,
            subtasks_str,
        )

    console.print(table)
    console.print(
        "\n[dim]Run [bold]tasks.py next[/] to pick the top priority ready task[/]"
    )


@cli.command("next")
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help="Output as JSON for machine consumption",
)
def next_(output_json):
    """Show the highest priority ready (unblocked) task.

    Picks from new or active tasks that have no dependencies
    or whose dependencies are all completed.

    Use --json for machine-readable output in autonomous workflows.
    """
    console = Console()
    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    # Load all tasks
    all_tasks = load_tasks(tasks_dir)
    if not all_tasks:
        console.print("[yellow]No tasks found![/]")
        return

    # Create task lookup dictionary
    tasks_dict = {task.name: task for task in all_tasks}

    # Filter for new or active tasks
    workable_tasks = [task for task in all_tasks if task.state in ["new", "active"]]
    if not workable_tasks:
        console.print("[yellow]No new or active tasks found![/]")
        return

    # Filter for ready (unblocked) tasks
    ready_tasks = [task for task in workable_tasks if is_task_ready(task, tasks_dict)]
    if not ready_tasks:
        if output_json:
            print(
                json.dumps(
                    {
                        "next_task": None,
                        "alternatives": [],
                        "error": "No ready tasks found",
                    },
                    indent=2,
                )
            )
            return
        console.print("[yellow]No ready tasks found![/]")
        console.print("\n[dim]All new/active tasks are blocked by dependencies.[/]")
        console.print(
            "[dim]Run [bold]tasks.py ready --state both[/] to see all ready work[/]"
        )
        return

    # Sort tasks by priority (high to low) and then by creation date (oldest first)
    ready_tasks.sort(
        key=lambda t: (
            -t.priority_rank,
            t.created,
        )
    )

    # Get the highest priority ready task
    next_task = ready_tasks[0]

    # JSON output for machine consumption
    if output_json:
        result = {
            "next_task": task_to_dict(next_task),
            "alternatives": [
                task_to_dict(t) for t in ready_tasks[1:4]
            ],  # Top 3 alternatives
        }
        print(json.dumps(result, indent=2))
        return

    # Show task using same format as show command
    console.print(
        f"\n[bold blue]🏃 Next Task:[/] (Priority: {next_task.priority or 'none'})"
    )
    # Call show command directly instead of using callback
    show(next_task.name)


@cli.command("stale")
@click.option(
    "--days",
    default=30,
    type=int,
    help="Number of days without modification to consider stale (default: 30)",
)
@click.option(
    "--state",
    type=click.Choice(["active", "paused", "new", "all"]),
    default="active",
    help="Filter by task state (default: active)",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help="Output as JSON for machine consumption",
)
def stale(days: int, state: str, output_json: bool):
    """List stale tasks that haven't been modified recently.

    Identifies tasks that may need review for completion, archival, or reassessment.
    By default shows active tasks not modified in 30+ days.

    Examples:
        tasks.py stale                    # Active tasks unchanged for 30+ days
        tasks.py stale --days 60          # Active tasks unchanged for 60+ days
        tasks.py stale --state all        # All tasks regardless of state
        tasks.py stale --state paused     # Only paused stale tasks
        tasks.py stale --json             # Machine-readable output
    """
    console = Console()

    # Validate days parameter
    if days <= 0:
        if output_json:
            print(json.dumps({"error": "--days must be a positive integer"}, indent=2))
            raise SystemExit(1)
        console.print("[red]Error: --days must be a positive integer[/]")
        raise SystemExit(1)

    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    # Load all tasks
    all_tasks = load_tasks(tasks_dir)
    if not all_tasks:
        if output_json:
            print(json.dumps({"stale_tasks": [], "count": 0}, indent=2))
            return
        console.print("[yellow]No tasks found![/]")
        return

    # Calculate cutoff date
    cutoff = datetime.now() - timedelta(days=days)

    # Filter by state
    if state == "all":
        state_filtered = all_tasks
    else:
        state_filtered = [task for task in all_tasks if task.state == state]

    # Filter for stale tasks (not modified since cutoff)
    stale_tasks = [task for task in state_filtered if task.modified < cutoff]

    if not stale_tasks:
        if output_json:
            print(
                json.dumps(
                    {"stale_tasks": [], "count": 0, "days_threshold": days}, indent=2
                )
            )
            return
        console.print(
            f"[green]No stale tasks found![/] (threshold: {days} days, state: {state})"
        )
        return

    # Sort by modification date (oldest first)
    stale_tasks.sort(key=lambda t: t.modified)

    # JSON output for machine consumption
    if output_json:
        result = {
            "stale_tasks": [
                {
                    **task_to_dict(t),
                    "days_since_modified": (datetime.now() - t.modified).days,
                }
                for t in stale_tasks
            ],
            "count": len(stale_tasks),
            "days_threshold": days,
            "state_filter": state,
        }
        print(json.dumps(result, indent=2))
        return

    # Create table
    table = Table(
        title=f"[bold yellow]Stale Tasks[/] ({len(stale_tasks)} unchanged for {days}+ days)"
    )
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("State", style="blue")
    table.add_column("Days Stale", style="red", justify="right")
    table.add_column("Task", style="white")
    table.add_column("Progress", style="magenta")
    table.add_column("Priority", style="yellow")

    # Create stable enumerated ID mapping
    tasks_by_date = sorted(all_tasks, key=lambda t: t.created)
    name_to_enum_id = {task.name: i for i, task in enumerate(tasks_by_date, 1)}

    for task in stale_tasks:
        enum_id = name_to_enum_id[task.name]
        state_emoji = STATE_EMOJIS.get(task.state or "untracked", "•")
        priority_emoji = STATE_EMOJIS.get(task.priority or "", "")
        days_stale = (datetime.now() - task.modified).days

        # Progress display
        if task.subtasks.total > 0:
            pct = int(task.subtasks.completed / task.subtasks.total * 100)
            progress_str = f"{pct}% ({task.subtasks.completed}/{task.subtasks.total})"
        else:
            progress_str = "-"

        table.add_row(
            str(enum_id),
            state_emoji,
            str(days_stale),
            task.name,
            progress_str,
            priority_emoji or (task.priority or "-"),
        )

    console.print(table)
    console.print(
        "\n[dim]Review these tasks for: completion, archival, or reassessment[/]"
    )
    console.print("[dim]Run [bold]tasks.py show <id>[/] to inspect a task's details[/]")


@cli.command("sync")
@click.option(
    "--update",
    is_flag=True,
    help="Update task states to match GitHub issue states",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help="Output as JSON for machine consumption",
)
def sync(update, output_json):
    """Sync task states with linked GitHub issues.

    Finds tasks with tracking field in frontmatter and compares
    their state with the linked GitHub issue state.

    Use --update to automatically update task states to match issue states.
    Use --json for machine-readable output.

    GitHub state mapping:
    - OPEN issue -> task should be active/new
    - CLOSED issue -> task should be done
    """
    console = Console()
    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    # Load all tasks
    all_tasks = load_tasks(tasks_dir)
    if not all_tasks:
        console.print("[yellow]No tasks found![/]")
        return

    # Find tasks with tracking field
    tasks_with_tracking = []
    for task in all_tasks:
        tracking = task.metadata.get("tracking")
        if tracking:
            if isinstance(tracking, list):
                # Add task once per tracked URL
                for track_url in tracking:
                    tasks_with_tracking.append((task, track_url))
            else:
                tasks_with_tracking.append((task, tracking))

    if not tasks_with_tracking:
        if output_json:
            print(
                json.dumps(
                    {
                        "synced_tasks": [],
                        "count": 0,
                        "message": "No tasks with tracking field found",
                    },
                    indent=2,
                )
            )
            return
        console.print("[yellow]No tasks with tracking field found![/]")
        console.print(
            "\n[dim]Add tracking to frontmatter: tracking: ['https://github.com/owner/repo/issues/123'][/]"
        )
        return

    # Check each task against GitHub/Linear
    results = []
    for task, tracking_ref in tasks_with_tracking:
        # Parse tracking reference (supports GitHub and Linear URLs)
        issue_info = parse_tracking_ref(tracking_ref)
        if not issue_info:
            results.append(
                {
                    "task": task.name,
                    "tracking": tracking_ref,
                    "error": "Could not parse tracking reference",
                    "task_state": task.state,
                    "issue_state": None,
                    "in_sync": False,
                }
            )
            continue

        # Fetch issue state based on source
        source = issue_info.get("source", "github")
        if source == "linear":
            issue_state = fetch_linear_issue_state(issue_info["identifier"])
            error_msg = "Could not fetch issue state from Linear"
        else:
            issue_state = fetch_github_issue_state(issue_info["repo"], issue_info["number"])
            error_msg = "Could not fetch issue state from GitHub"

        if issue_state is None:
            results.append(
                {
                    "task": task.name,
                    "tracking": tracking_ref,
                    "error": error_msg,
                    "task_state": task.state,
                    "issue_state": None,
                    "in_sync": False,
                }
            )
            continue

        # Determine expected task state based on issue state
        # GitHub uses OPEN/CLOSED, Linear uses state types (completed, canceled, started, etc.)
        is_closed = issue_state in ["CLOSED", "completed", "canceled"]
        is_open = issue_state in ["OPEN", "started", "triage", "backlog", "unstarted", "in_progress"]

        expected_state = "done" if is_closed else (task.state or "active")
        if is_open and task.state == "done":
            expected_state = "active"  # Reopened issue

        in_sync = (is_closed and task.state == "done") or (
            is_open and task.state in ["new", "active", "paused"]
        )

        result = {
            "task": task.name,
            "tracking": tracking_ref,
            "task_state": task.state,
            "issue_state": issue_state,
            "expected_state": expected_state,
            "in_sync": in_sync,
        }

        # Update task if requested and out of sync
        if update and not in_sync:
            if update_task_state(task.path, expected_state):
                result["updated"] = True
                result["new_state"] = expected_state
            else:
                result["error"] = "Failed to update task file"

        results.append(result)

    # Output results
    if output_json:
        output = {
            "synced_tasks": results,
            "count": len(results),
            "in_sync": sum(1 for r in results if r.get("in_sync", False)),
            "out_of_sync": sum(1 for r in results if not r.get("in_sync", False)),
        }
        if update:
            output["updated"] = sum(1 for r in results if r.get("updated", False))
        print(json.dumps(output, indent=2))
        return

    # Rich table output
    table = Table(title=f"[bold]Task-Issue Sync Status[/] ({len(results)} tracked)")
    table.add_column("Task", style="cyan")
    table.add_column("Issue", style="blue")
    table.add_column("Task State", style="yellow")
    table.add_column("Issue State", style="green")
    table.add_column("Status", style="white")

    for result in results:
        if result.get("error"):
            status = f"[red]Error: {result['error']}[/]"
        elif result.get("in_sync"):
            status = "[green]✓ In sync[/]"
        elif result.get("updated"):
            status = f"[blue]→ Updated to {result['new_state']}[/]"
        else:
            status = f"[yellow]⚠ Out of sync (expected: {result.get('expected_state', 'unknown')})[/]"

        table.add_row(
            result["task"][:30],
            result.get("tracking", "")[:25],
            result.get("task_state", ""),
            result.get("issue_state", "N/A"),
            status,
        )

    console.print(table)

    out_of_sync = sum(
        1 for r in results if not r.get("in_sync", False) and not r.get("error")
    )
    if out_of_sync > 0 and not update:
        console.print(
            f"\n[dim]Run with --update to sync {out_of_sync} out-of-sync tasks[/]"
        )


def parse_tracking_ref(ref: str) -> Optional[Dict[str, str]]:
    """Parse tracking reference to extract repo/team and issue number.

    Supports formats:
    - owner/repo#123 (GitHub shorthand)
    - https://github.com/owner/repo/issues/123
    - https://github.com/owner/repo/pull/123
    - https://linear.app/team/issue/IDENTIFIER (Linear)

    Returns dict with 'source' ('github' or 'linear'), plus source-specific fields.
    """
    # GitHub full URL format
    github_url_match = re.match(r"https://github\.com/([^/]+/[^/]+)/(issues|pull)/(\d+)", ref)
    if github_url_match:
        return {"source": "github", "repo": github_url_match.group(1), "number": github_url_match.group(3)}

    # GitHub short format: owner/repo#123
    github_short_match = re.match(r"([^/]+/[^#]+)#(\d+)", ref)
    if github_short_match:
        return {"source": "github", "repo": github_short_match.group(1), "number": github_short_match.group(2)}

    # Linear URL format: https://linear.app/team/issue/IDENTIFIER
    linear_match = re.match(r"https://linear\.app/([^/]+)/issue/([^/]+)", ref)
    if linear_match:
        return {"source": "linear", "team": linear_match.group(1), "identifier": linear_match.group(2)}

    return None


def fetch_github_issue_state(repo: str, number: str) -> Optional[str]:
    """Fetch GitHub issue/PR state using gh CLI."""
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                number,
                "--repo",
                repo,
                "--json",
                "state",
                "-q",
                ".state",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        # Try as PR if issue fails
        result = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                number,
                "--repo",
                repo,
                "--json",
                "state",
                "-q",
                ".state",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, Exception):
        pass
    return None


def fetch_linear_issue_state(identifier: str) -> Optional[str]:
    """Fetch Linear issue state using GraphQL API.

    Args:
        identifier: Linear issue identifier (e.g., 'SUDO-123')

    Returns:
        Issue state type (e.g., 'started', 'completed', 'canceled') or None if failed
    """
    import os

    token = os.environ.get("LOFTY_LINEAR_TOKEN") or os.environ.get("LINEAR_API_KEY")
    if not token:
        return None

    query = """
    query($identifier: String!) {
        issue(id: $identifier) {
            state { type }
        }
    }
    """

    # Linear identifiers like SUDO-123 need to be looked up differently
    # Try issueViaIdOrKey first
    query = """
    query($id: String!) {
        issueViaIdentifier: issue(id: $id) {
            state { type }
        }
    }
    """

    try:
        import urllib.request

        # First try by identifier (SUDO-123)
        search_query = """
        query($filter: IssueFilter!) {
            issues(filter: $filter, first: 1) {
                nodes {
                    state { type }
                }
            }
        }
        """

        req = urllib.request.Request(
            "https://api.linear.app/graphql",
            data=json.dumps({
                "query": search_query,
                "variables": {"filter": {"identifier": {"eq": identifier}}}
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": token,
            },
        )

        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())

        if "errors" not in data:
            issues = data.get("data", {}).get("issues", {}).get("nodes", [])
            if issues:
                state_type = issues[0].get("state", {}).get("type")
                return str(state_type) if state_type else None
    except Exception:
        pass

    return None


def update_task_state(task_path: Path, new_state: str) -> bool:
    """Update task frontmatter state field."""
    try:
        post = frontmatter.load(task_path)
        post["state"] = new_state
        with open(task_path, "w") as f:
            f.write(frontmatter.dumps(post))
        return True
    except Exception:
        return False


@cli.command()
@click.argument("task_id")
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help="Output as JSON for machine consumption",
)
def plan(task_id: str, output_json: bool):
    """Show the impact of completing a task.

    Analyzes what tasks would be unblocked if the specified task is completed.
    Useful for prioritizing work based on impact.

    The TASK_ID can be the numeric ID or task name.

    Examples:
        tasks.py plan 5                   # Impact analysis for task #5
        tasks.py plan my-task-name        # By task name
        tasks.py plan 5 --json            # Machine-readable output
    """
    console = Console()

    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    # Load all tasks
    all_tasks = load_tasks(tasks_dir)
    if not all_tasks:
        if output_json:
            print(json.dumps({"error": "No tasks found"}, indent=2))
            raise SystemExit(1)
        console.print("[red]No tasks found![/]")
        raise SystemExit(1)

    # Create stable enumerated ID mapping
    tasks_by_date = sorted(all_tasks, key=lambda t: t.created)
    name_to_enum_id = {task.name: i for i, task in enumerate(tasks_by_date, 1)}
    enum_id_to_task = {i: task for i, task in enumerate(tasks_by_date, 1)}

    # Resolve task_id to actual task
    target_task = None

    # Try as numeric ID first
    try:
        numeric_id = int(task_id)
        if numeric_id in enum_id_to_task:
            target_task = enum_id_to_task[numeric_id]
    except ValueError:
        pass

    # Try as task name
    if target_task is None:
        for task in all_tasks:
            if task.name == task_id or task.name == task_id.replace("-", "_"):
                target_task = task
                break

    if target_task is None:
        if output_json:
            print(json.dumps({"error": f"Task '{task_id}' not found"}, indent=2))
            raise SystemExit(1)
        console.print(f"[red]Task '{task_id}' not found![/]")
        raise SystemExit(1)

    # Find all tasks that depend on this task (reverse dependency lookup)
    dependent_tasks = []
    for task in all_tasks:
        if target_task.name in task.depends:
            dependent_tasks.append(task)

    # Calculate impact score
    # Scoring:
    # - Base: 1 point per dependent task
    # - Priority bonus: high=3, medium=2, low=1
    # - State bonus: active=2, new=1 (these would benefit most from unblocking)
    impact_score = 0.0
    priority_weights = {"high": 3, "medium": 2, "low": 1}
    state_weights = {"active": 2, "new": 1}

    impact_details = []
    for dep_task in dependent_tasks:
        task_impact = 1.0  # Base score
        task_impact += priority_weights.get(dep_task.priority or "", 0)
        task_impact += state_weights.get(dep_task.state or "", 0)
        impact_score += task_impact
        impact_details.append(
            {
                "task": dep_task.name,
                "state": dep_task.state or "unknown",
                "priority": dep_task.priority or "none",
                "impact_contribution": task_impact,
            }
        )

    # Check if this task has unmet dependencies itself
    unmet_dependencies = []
    for dep_name in target_task.depends:
        for task in all_tasks:
            if task.name == dep_name and task.state not in ["done", "cancelled"]:
                unmet_dependencies.append(
                    {"task": task.name, "state": task.state or "unknown"}
                )
                break

    # JSON output
    if output_json:
        result = {
            "task": target_task.name,
            "task_id": name_to_enum_id[target_task.name],
            "state": target_task.state or "unknown",
            "priority": target_task.priority or "none",
            "impact_analysis": {
                "score": round(impact_score, 1),
                "would_unblock": len(dependent_tasks),
                "dependent_tasks": impact_details,
            },
            "blockers": {
                "has_unmet_dependencies": len(unmet_dependencies) > 0,
                "unmet_dependencies": unmet_dependencies,
            },
        }
        print(json.dumps(result, indent=2))
        return

    # Rich console output
    target_id = name_to_enum_id[target_task.name]
    state_emoji = STATE_EMOJIS.get(target_task.state or "untracked", "•")

    console.print(f"\n[bold cyan]Impact Analysis: {target_task.name}[/] (#{target_id})")
    console.print(f"State: {state_emoji} {target_task.state or 'unknown'}")
    console.print(f"Priority: {target_task.priority or 'none'}")

    # Show unmet dependencies (blockers for this task)
    if unmet_dependencies:
        console.print(
            f"\n[bold red]⚠ Blocked by {len(unmet_dependencies)} unmet dependencies:[/]"
        )
        for dep in unmet_dependencies:
            dep_emoji = STATE_EMOJIS.get(dep["state"], "•")
            console.print(f"  - {dep_emoji} {dep['task']} ({dep['state']})")

    # Show what would be unblocked
    console.print(f"\n[bold green]Impact Score: {impact_score:.1f}[/]")

    if dependent_tasks:
        console.print(
            f"\n[bold]Completing this task would unblock {len(dependent_tasks)} task(s):[/]"
        )

        table = Table()
        table.add_column("Task", style="white")
        table.add_column("State", style="blue")
        table.add_column("Priority", style="yellow")
        table.add_column("Impact", style="green", justify="right")

        for detail in impact_details:
            task_emoji = STATE_EMOJIS.get(str(detail["state"]), "•")
            table.add_row(
                str(detail["task"]),
                f"{task_emoji} {detail['state']}",
                str(detail["priority"]),
                f"+{detail['impact_contribution']:.1f}",
            )

        console.print(table)
    else:
        console.print("\n[dim]No tasks depend on this task (leaf node).[/]")

    # Summary recommendation
    console.print()
    if impact_score >= 5:
        console.print(
            "[bold green]High impact![/] Completing this task will significantly unblock progress."
        )
    elif impact_score >= 2:
        console.print(
            "[yellow]Moderate impact.[/] Consider prioritizing if other high-impact tasks are blocked."
        )
    else:
        console.print("[dim]Low impact.[/] This is a leaf task or has few dependents.")


@cli.command("import")
@click.option(
    "--source",
    type=click.Choice(["github", "linear"]),
    required=True,
    help="Source to import from (github or linear)",
)
@click.option(
    "--repo",
    help="GitHub repository in owner/repo format (required for github source)",
)
@click.option(
    "--team",
    help="Linear team key (required for linear source)",
)
@click.option(
    "--state",
    type=click.Choice(["open", "closed", "all"]),
    default="open",
    help="Filter by issue state",
)
@click.option(
    "--label",
    multiple=True,
    help="Filter by label (can be used multiple times)",
)
@click.option(
    "--assignee",
    help="Filter by assignee (GitHub username or 'me')",
)
@click.option(
    "--limit",
    default=20,
    help="Maximum number of issues to import",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be imported without creating files",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help="Output as JSON for machine consumption",
)
def import_issues(source, repo, team, state, label, assignee, limit, dry_run, output_json):
    """Import issues from GitHub or Linear as placeholder tasks.

    Creates minimal task files with tracking frontmatter linking back
    to the source. Existing tasks with matching tracking URLs are skipped
    to avoid duplicates.

    \b
    Examples:
        # Import open issues from a GitHub repo
        tasks.py import --source github --repo gptme/gptme --state open

        # Import issues with specific labels
        tasks.py import --source github --repo gptme/gptme --label bug --label priority:high

        # Import issues assigned to you
        tasks.py import --source github --repo gptme/gptme --assignee me

        # Dry run to preview imports
        tasks.py import --source github --repo gptme/gptme --dry-run

        # Import from Linear (requires LOFTY_LINEAR_TOKEN)
        tasks.py import --source linear --team ENG
    """
    console = Console()
    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    # Validate source-specific options
    if source == "github" and not repo:
        console.print("[red]Error: --repo is required for github source[/]")
        sys.exit(1)
    if source == "linear" and not team:
        console.print("[red]Error: --team is required for linear source[/]")
        sys.exit(1)

    # Ensure tasks directory exists
    tasks_dir.mkdir(parents=True, exist_ok=True)

    # Load existing tasks to check for duplicates
    existing_tasks = load_tasks(tasks_dir)
    existing_tracking = set()
    for task in existing_tasks:
        tracking = task.metadata.get("tracking")
        if tracking:
            if isinstance(tracking, list):
                existing_tracking.update(tracking)
            else:
                existing_tracking.add(tracking)

    # Fetch issues based on source
    if source == "github":
        issues = fetch_github_issues(repo, state, list(label), assignee, limit)
    else:  # linear
        issues = fetch_linear_issues(team, state, limit)

    if not issues:
        if output_json:
            print(json.dumps({"imported": [], "count": 0, "message": "No issues found"}, indent=2))
            return
        console.print("[yellow]No issues found matching criteria[/]")
        return

    # Process issues
    imported = []
    skipped = []
    for issue in issues:
        tracking_ref = issue["tracking_ref"]

        # Check for duplicates
        if tracking_ref in existing_tracking:
            skipped.append({
                "title": issue["title"],
                "tracking_ref": tracking_ref,
                "reason": "Already exists in tasks",
            })
            continue

        # Generate task filename
        task_filename = generate_task_filename(issue["title"], issue["number"], source)
        task_path = tasks_dir / task_filename

        # Skip if file already exists (belt and suspenders)
        if task_path.exists():
            skipped.append({
                "title": issue["title"],
                "tracking_ref": tracking_ref,
                "reason": f"File {task_filename} already exists",
            })
            continue

        # Map priority from labels if available
        priority = map_priority_from_labels(issue.get("labels", []))

        # Generate task content
        task_content = generate_task_content(issue, source, priority)

        if dry_run:
            imported.append({
                "title": issue["title"],
                "tracking_ref": tracking_ref,
                "filename": task_filename,
                "dry_run": True,
            })
        else:
            # Create the task file
            try:
                task_path.write_text(task_content)
                imported.append({
                    "title": issue["title"],
                    "tracking_ref": tracking_ref,
                    "filename": task_filename,
                    "created": True,
                })
            except Exception as e:
                skipped.append({
                    "title": issue["title"],
                    "tracking_ref": tracking_ref,
                    "reason": f"Failed to create file: {e}",
                })

    # Output results
    if output_json:
        result = {
            "imported": imported,
            "skipped": skipped,
            "count": len(imported),
            "skipped_count": len(skipped),
        }
        if dry_run:
            result["dry_run"] = True
        print(json.dumps(result, indent=2))
        return

    # Rich output
    if dry_run:
        console.print("[bold yellow]DRY RUN[/] - No files created\n")

    if imported:
        table = Table(title=f"[bold]{'Would Import' if dry_run else 'Imported'} ({len(imported)} issues)[/]")
        table.add_column("Title", style="cyan", max_width=50)
        table.add_column("Tracking", style="blue")
        table.add_column("Filename", style="green")

        for item in imported:
            table.add_row(
                item["title"][:50],
                item["tracking_ref"],
                item["filename"],
            )
        console.print(table)

    if skipped:
        console.print(f"\n[yellow]Skipped {len(skipped)} issues:[/]")
        for item in skipped:
            console.print(f"  - {item['title'][:40]}: {item['reason']}")

    if not dry_run and imported:
        console.print(f"\n[green]✓ Created {len(imported)} task files in {tasks_dir}[/]")
        console.print("[dim]Run 'tasks.py sync' to keep states synchronized[/]")


def fetch_github_issues(repo: str, state: str, labels: List[str], assignee: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Fetch issues from GitHub using gh CLI.

    Args:
        repo: Repository in owner/repo format
        state: Issue state filter (open, closed, all)
        labels: List of labels to filter by
        assignee: Filter by assignee (use 'me' for authenticated user)
        limit: Maximum number of issues to fetch

    Returns:
        List of issue dicts with keys: number, title, state, labels, url, body, tracking_ref, source
    """
    cmd = [
        "gh", "issue", "list",
        "--repo", repo,
        "--limit", str(limit),
        "--json", "number,title,state,labels,url,body",
    ]

    if state != "all":
        cmd.extend(["--state", state])

    for label in labels:
        cmd.extend(["--label", label])

    if assignee:
        cmd.extend(["--assignee", assignee])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return []

        issues_data = json.loads(result.stdout)
        issues = []
        for issue in issues_data:
            issues.append({
                "number": issue["number"],
                "title": issue["title"],
                "state": issue["state"].lower(),
                "labels": [l["name"] for l in issue.get("labels", [])],
                "url": issue["url"],
                "body": issue.get("body", "")[:500] if issue.get("body") else "",
                "tracking_ref": issue["url"],  # Use full URL, same as Linear
                "source": "github",
            })
        return issues
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
        return []


def fetch_linear_issues(team: str, state: str, limit: int) -> List[Dict[str, Any]]:
    """Fetch issues from Linear using GraphQL API.

    Requires LOFTY_LINEAR_TOKEN or LINEAR_API_KEY environment variable.

    Args:
        team: Linear team key (e.g., 'ENG', 'SUDO')
        state: Issue state filter (open, closed, all)
        limit: Maximum number of issues to fetch

    Returns:
        List of issue dicts with keys: number, title, state, labels, url, body, tracking_ref, source
    """
    import os

    token = os.environ.get("LOFTY_LINEAR_TOKEN") or os.environ.get("LINEAR_API_KEY")
    if not token:
        return []

    # Build state filter
    state_filter = {}
    if state == "open":
        state_filter = {"state": {"type": {"nin": ["completed", "canceled"]}}}
    elif state == "closed":
        state_filter = {"state": {"type": {"in": ["completed", "canceled"]}}}

    query = """
    query($teamKey: String!, $first: Int!, $filter: IssueFilter) {
        team(key: $teamKey) {
            issues(first: $first, filter: $filter, orderBy: updatedAt) {
                nodes {
                    identifier
                    title
                    state { name type }
                    labels { nodes { name } }
                    url
                    description
                }
            }
        }
    }
    """

    variables = {
        "teamKey": team,
        "first": limit,
        "filter": state_filter if state_filter else None,
    }

    try:
        import urllib.request

        req = urllib.request.Request(
            "https://api.linear.app/graphql",
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": token,
            },
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())

        if "errors" in data:
            return []

        team_data = data.get("data", {}).get("team")
        if not team_data:
            return []

        issues = []
        for issue in team_data.get("issues", {}).get("nodes", []):
            issues.append({
                "number": issue["identifier"],
                "title": issue["title"],
                "state": issue["state"]["type"] if issue.get("state") else "unknown",
                "labels": [l["name"] for l in issue.get("labels", {}).get("nodes", [])],
                "url": issue["url"],
                "body": (issue.get("description") or "")[:500],
                "tracking_ref": issue["url"],  # Use full URL for Linear
                "source": "linear",
            })
        return issues
    except Exception:
        return []


def generate_task_filename(title: str, number: Union[str, int], source: str) -> str:
    """Generate a task filename from title and number."""
    # Sanitize title for filename
    safe_title = re.sub(r'[^\w\s-]', '', title.lower())
    safe_title = re.sub(r'[-\s]+', '-', safe_title).strip('-')
    safe_title = safe_title[:50]  # Limit length

    if source == "linear":
        # Linear identifiers are like "ENG-123"
        return f"linear-{str(number).lower()}-{safe_title}.md"
    else:
        return f"gh-{number}-{safe_title}.md"


def map_priority_from_labels(labels: List[str]) -> Optional[str]:
    """Map labels to task priority."""
    labels_lower = [l.lower() for l in labels]

    if any(p in labels_lower for p in ["priority:high", "priority: high", "p0", "p1", "urgent", "critical"]):
        return "high"
    elif any(p in labels_lower for p in ["priority:medium", "priority: medium", "p2"]):
        return "medium"
    elif any(p in labels_lower for p in ["priority:low", "priority: low", "p3", "p4"]):
        return "low"

    return None


def generate_task_content(issue: Dict[str, Any], source: str, priority: Optional[str]) -> str:
    """Generate task file content from issue data."""
    # Map issue state to task state
    if issue["state"] in ["closed", "completed", "canceled"]:
        task_state = "done"
    else:
        task_state = "new"

    # Build frontmatter
    frontmatter_lines = [
        "---",
        f"state: {task_state}",
        f"created: {datetime.now().strftime('%Y-%m-%d')}",
        f"tracking: ['{issue['tracking_ref']}']",  # List format, full URL
    ]

    if priority:
        frontmatter_lines.append(f"priority: {priority}")

    # Add source-specific tags
    tags = []
    if source == "github":
        tags.append("github")
    elif source == "linear":
        tags.append("linear")

    # Add labels as tags (sanitized)
    for label in issue.get("labels", [])[:5]:  # Limit to 5 labels
        safe_label = re.sub(r'[^\w-]', '-', label.lower()).strip('-')
        if safe_label and safe_label not in tags:
            tags.append(safe_label)

    if tags:
        frontmatter_lines.append(f"tags: [{', '.join(tags)}]")

    frontmatter_lines.append("---")

    # Build body
    body_lines = [
        "",
        f"# {issue['title']}",
        "",
        f"**Source**: [{source.capitalize()} #{issue['number']}]({issue['url']})",
        "",
    ]

    if issue.get("body"):
        body_lines.extend([
            "## Description",
            "",
            issue["body"],
            "",
        ])

    body_lines.extend([
        "## Notes",
        "",
        "*Imported from external tracker. See source link for full context.*",
        "",
    ])

    return "\n".join(frontmatter_lines) + "\n".join(body_lines)


if __name__ == "__main__":
    cli()
