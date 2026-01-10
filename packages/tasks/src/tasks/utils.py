"""Utility functions and data structures for task management.

This module contains shared utilities used by the tasks CLI.
Note: Uses absolute imports for uv script compatibility.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    NamedTuple,
    Optional,
    Tuple,
)

import frontmatter

logger = logging.getLogger(__name__)


# === Constants ===

VALID_STATES = ["new", "active", "paused", "done", "cancelled", "someday"]
VALID_PRIORITIES = ["urgent", "high", "medium", "low", None]

# Priority rank for sorting (higher = more important)
PRIORITY_RANK: Dict[Optional[str], int] = {
    "urgent": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
    None: 0,
}


# === Data Structures ===


@dataclass
class DirectoryConfig:
    """Configuration for a directory type."""

    type_name: str
    states: list[str]
    special_files: list[str]
    emoji: str


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

    Attributes:
        path: Path to the task file
        name: Filename without .md extension
        state: Current state from frontmatter
        created: Creation timestamp
        modified: Last modification timestamp
        priority: Task priority
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
    metadata: Dict[str, Any]

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
        """Get numeric priority rank for sorting."""
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


# === Helper Functions ===


def format_time_ago(dt: datetime) -> str:
    """Format datetime as human-readable 'time ago' string."""
    now = datetime.now()
    if dt.tzinfo:
        now = datetime.now(dt.tzinfo)

    diff = now - dt
    seconds = diff.total_seconds()

    if seconds < 60:
        return "just now"
    elif seconds < 3600:
        minutes = int(seconds / 60)
        return f"{minutes}m ago"
    elif seconds < 86400:
        hours = int(seconds / 3600)
        return f"{hours}h ago"
    elif seconds < 604800:
        days = int(seconds / 86400)
        return f"{days}d ago"
    elif seconds < 2592000:
        weeks = int(seconds / 604800)
        return f"{weeks}w ago"
    elif seconds < 31536000:
        months = int(seconds / 2592000)
        return f"{months}mo ago"
    else:
        years = int(seconds / 31536000)
        return f"{years}y ago"


def count_subtasks(content: str) -> SubtaskCount:
    """Count completed and total subtasks in markdown content.

    Looks for markdown task list items:
    - [ ] Incomplete task
    - [x] Completed task
    - ✅ Completed task
    """
    # Pattern for checkbox items
    checkbox_pattern = r"^\s*-\s*\[([ x])\]"
    # Pattern for emoji checkmark items
    emoji_pattern = r"^\s*-\s*(✅|☑️)"

    total = 0
    completed = 0

    for line in content.split("\n"):
        # Check for checkbox items
        checkbox_match = re.match(checkbox_pattern, line, re.IGNORECASE)
        if checkbox_match:
            total += 1
            if checkbox_match.group(1).lower() == "x":
                completed += 1
            continue

        # Check for emoji items
        if re.match(emoji_pattern, line):
            total += 1
            completed += 1

    return SubtaskCount(completed=completed, total=total)


def find_repo_root(start_path: Path) -> Path:
    """Find the repository root by looking for .git directory."""
    current = start_path.resolve()
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    return start_path.resolve()


def validate_task_file(file: Path, post: frontmatter.Post) -> List[str]:
    """Validate a task file and return list of issues.

    Checks:
    - Required metadata fields (state, created)
    - Valid state values
    - Valid priority values
    - Created date is parseable
    """
    issues = []
    metadata = post.metadata

    # Check required fields
    if "state" not in metadata:
        issues.append("Missing required field: state")
    elif metadata["state"] not in VALID_STATES:
        issues.append(f"Invalid state: {metadata['state']}")

    if "created" not in metadata:
        issues.append("Missing required field: created")
    else:
        created = metadata["created"]
        if not isinstance(created, datetime):
            try:
                datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                issues.append(f"Invalid created date format: {created}")

    # Check optional fields
    if "priority" in metadata and metadata["priority"] not in VALID_PRIORITIES:
        issues.append(f"Invalid priority: {metadata['priority']}")

    # Check depends format
    if "depends" in metadata:
        depends = metadata["depends"]
        if not isinstance(depends, list):
            issues.append("'depends' should be a list")

    # Check tags format
    if "tags" in metadata:
        tags = metadata["tags"]
        if not isinstance(tags, list):
            issues.append("'tags' should be a list")

    return issues


def parse_datetime(value: Any) -> datetime:
    """Parse a datetime value from frontmatter.

    Handles:
    - datetime objects (returned as-is)
    - ISO format strings
    - Falls back to current time on failure
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now()


def load_task_file(file: Path) -> Tuple[frontmatter.Post, SubtaskCount]:
    """Load a task file and return the parsed frontmatter and subtask count."""
    post = frontmatter.load(file)
    subtasks = count_subtasks(post.content)
    return post, subtasks


def task_to_dict(task: TaskInfo) -> Dict[str, Any]:
    """Convert TaskInfo to dictionary for JSON serialization."""
    return {
        "name": task.name,
        "path": str(task.path),
        "state": task.state,
        "priority": task.priority,
        "created": task.created.isoformat(),
        "modified": task.modified.isoformat(),
        "tags": task.tags,
        "depends": task.depends,
        "subtasks": {
            "completed": task.subtasks.completed,
            "total": task.subtasks.total,
        },
        "has_issues": task.has_issues,
        "issues": task.issues if task.issues else None,
    }
