"""Utility functions and classes for the tasks package.

This module contains:
- Data classes: DirectoryConfig, SubtaskCount, TaskInfo, StateChecker
- Constants: CONFIGS, PRIORITY_RANK, STATE_STYLES, STATE_EMOJIS
- Task loading and validation utilities
- Cache management helpers
- GitHub API helpers
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    NamedTuple,
    Optional,
    Tuple,
)

if TYPE_CHECKING:
    import frontmatter as fm

# Lazy import frontmatter to avoid import issues in uv scripts
_frontmatter = None


def _get_frontmatter():
    """Lazy import frontmatter."""
    global _frontmatter
    if _frontmatter is None:
        import frontmatter

        _frontmatter = frontmatter
    return _frontmatter


# =============================================================================
# Constants
# =============================================================================


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

PRIORITY_RANK: dict[str | None, int] = {
    "urgent": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
    None: 0,  # Tasks without priority
}

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


# =============================================================================
# Data Classes
# =============================================================================


class SubtaskCount(NamedTuple):
    """Count of completed and total subtasks."""

    completed: int
    total: int

    def __str__(self) -> str:
        """Return string representation like (4/16)."""
        return f"({self.completed}/{self.total})" if self.total > 0 else ""


@dataclass
class TaskInfo:
    """Information about a task with metadata and validation."""

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


# =============================================================================
# Basic Utility Functions
# =============================================================================


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


def count_subtasks(content: str) -> SubtaskCount:
    """Count completed and total subtasks in markdown content."""
    completed = len(re.findall(r"- (\[x\]|✅)", content))
    total = len(re.findall(r"- (\[ \]|🏃)", content)) + completed
    return SubtaskCount(completed, total)


# =============================================================================
# Task Validation and Loading
# =============================================================================


def validate_task_file(file: Path, post: "fm.Post") -> List[str]:
    """Validate a task file's format and required fields."""
    issues = []
    metadata = post.metadata

    required_fields: Dict[str, type | tuple[type, ...]] = {
        "state": str,
        "created": (str, datetime),
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

    if "state" in metadata:
        state = metadata["state"]
        if state not in CONFIGS["tasks"].states:
            issues.append(f"Invalid state: {state}")

    if "created" in metadata and isinstance(metadata["created"], str):
        try:
            datetime.fromisoformat(metadata["created"])
        except ValueError:
            try:
                from datetime import date

                date.fromisoformat(metadata["created"])
            except ValueError:
                issues.append(
                    "Created date must be ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)"
                )

    if "priority" in metadata:
        priority = metadata["priority"]
        if priority not in ("high", "medium", "low", None):
            issues.append("Priority must be 'high', 'medium', or 'low'")

    if "tags" in metadata and not isinstance(metadata["tags"], list):
        issues.append("Tags must be a list")

    if "depends" in metadata and not isinstance(metadata["depends"], list):
        issues.append("Dependencies must be a list")

    return issues


def load_task(file: Path) -> Tuple["fm.Post", SubtaskCount]:
    """Load a single task file and count its subtasks."""
    frontmatter = _get_frontmatter()
    post = frontmatter.load(file)
    subtasks = count_subtasks(post.content)
    return post, subtasks


def load_tasks(
    tasks_dir: Path, recursive: bool = False, single_file: Optional[Path] = None
) -> List[TaskInfo]:
    """Load tasks from directory or single file with metadata."""
    frontmatter = _get_frontmatter()
    tasks = []
    excluded_dirs = {"templates", "video-scripts", "agent-setup-interview"}

    if single_file:
        if not single_file.exists():
            logging.error(f"File not found: {single_file}")
            return []
        files = [single_file]
    else:
        pattern = "**/*.md" if recursive else "*.md"
        files = [
            f
            for f in tasks_dir.glob(pattern)
            if not recursive or not any(d in f.parts for d in excluded_dirs)
        ]

    for file in files:
        try:
            post = frontmatter.load(file)
            metadata = post.metadata
            issues = validate_task_file(file, post)
            subtasks = count_subtasks(post.content)

            state = metadata.get("state")
            if not state:
                issues.append("No state in frontmatter")
                state = "new"

            def parse_datetime_field(value) -> datetime:
                if isinstance(value, datetime):
                    return value
                value_str = str(value)
                try:
                    return datetime.fromisoformat(value_str)
                except ValueError:
                    from datetime import date

                    date_obj = date.fromisoformat(value_str)
                    return datetime.combine(date_obj, datetime.min.time())

            try:
                created = parse_datetime_field(metadata.get("created", ""))
                modified = parse_datetime_field(metadata.get("modified", ""))
            except (ValueError, TypeError):
                try:
                    result = subprocess.run(
                        ["git", "log", "-1", "--format=%at", "--", str(file)],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    timestamp = int(result.stdout.strip())
                    modified = datetime.fromtimestamp(timestamp)

                    result = subprocess.run(
                        ["git", "log", "--reverse", "--format=%at", "--", str(file)],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    timestamp = int(result.stdout.strip().split("\n")[0])
                    created = datetime.fromtimestamp(timestamp)
                except (subprocess.CalledProcessError, ValueError, IndexError):
                    stats = file.stat()
                    created = datetime.fromtimestamp(stats.st_ctime)
                    modified = datetime.fromtimestamp(stats.st_mtime)

            if created.tzinfo:
                created = created.astimezone().replace(tzinfo=None)
            if modified.tzinfo:
                modified = modified.astimezone().replace(tzinfo=None)

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


# =============================================================================
# Task Resolution and Ready Check
# =============================================================================


def task_to_dict(task: TaskInfo) -> Dict[str, Any]:
    """Serialize TaskInfo to a JSON-compatible dictionary."""
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


def is_task_ready(
    task: TaskInfo,
    all_tasks: Dict[str, TaskInfo],
    issue_cache: Optional[Dict[str, Any]] = None,
) -> bool:
    """Check if a task is ready (unblocked) to work on."""
    if not task.depends:
        if issue_cache:
            blocks = task.metadata.get("blocks", [])
            if isinstance(blocks, str):
                blocks = [blocks]
            for block in blocks:
                if isinstance(block, str) and block.startswith("http"):
                    cached = issue_cache.get(block)
                    if cached and cached.get("state") == "OPEN":
                        return False
        return True

    for dep_name in task.depends:
        dep_task = all_tasks.get(dep_name)
        if dep_task is None:
            return False
        if dep_task.state not in ["done", "cancelled"]:
            return False

    if issue_cache:
        blocks = task.metadata.get("blocks", [])
        if isinstance(blocks, str):
            blocks = [blocks]
        for block in blocks:
            if isinstance(block, str) and block.startswith("http"):
                cached = issue_cache.get(block)
                if cached and cached.get("state") == "OPEN":
                    return False

    return True


def resolve_tasks(
    task_ids: List[str], tasks: List[TaskInfo], tasks_dir: Path
) -> List[TaskInfo]:
    """Resolve tasks by ID/path, supporting both task names and paths."""
    matched_tasks = []
    for task_id in task_ids:
        task_path = Path(task_id)
        if task_path.suffix == ".md":
            repo_root = tasks_dir.parent
            paths_to_try = [
                task_path,
                tasks_dir / task_path,
                tasks_dir / task_path.name,
                repo_root / task_path,
            ]
            task = None
            for path in paths_to_try:
                task = next((t for t in tasks if t.path == path.resolve()), None)
                if task:
                    break
        else:
            task = next((t for t in tasks if t.name == task_id), None)

        if not task:
            raise ValueError(f"Task not found: {task_id}")
        matched_tasks.append(task)

    return matched_tasks


# =============================================================================
# State Checker Class
# =============================================================================


class StateChecker:
    """Check state directories for issues and status."""

    def __init__(self, repo_root: Path, config: DirectoryConfig):
        self.root = repo_root
        self.config = config
        self.base_dir = repo_root / config.type_name

    def check_all(self) -> Dict[str, List[TaskInfo]]:
        """Check all files and categorize by state."""
        results: Dict[str, List[TaskInfo]] = {
            "untracked": [],
            "issues": [],
        }
        for state in self.config.states:
            results[state] = []

        tasks = load_tasks(self.base_dir)

        for task in tasks:
            if task.path.name in self.config.special_files:
                continue

            if task.issues:
                results["issues"].append(task)
            elif not task.state:
                results["untracked"].append(task)
            else:
                results[task.state].append(task)

        return results


# =============================================================================
# GitHub API Helpers
# =============================================================================


def parse_tracking_ref(ref: str) -> Optional[Dict[str, str]]:
    """Parse tracking reference to extract repo and issue number."""
    url_match = re.match(r"https://github\.com/([^/]+/[^/]+)/(issues|pull)/(\d+)", ref)
    if url_match:
        return {"repo": url_match.group(1), "number": url_match.group(3)}

    short_match = re.match(r"([^/]+/[^#]+)#(\d+)", ref)
    if short_match:
        return {"repo": short_match.group(1), "number": short_match.group(2)}

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


def update_task_state(task_path: Path, new_state: str) -> bool:
    """Update task frontmatter state field."""
    frontmatter = _get_frontmatter()
    try:
        post = frontmatter.load(task_path)
        post["state"] = new_state
        with open(task_path, "w") as f:
            f.write(frontmatter.dumps(post))
        return True
    except Exception:
        return False


# =============================================================================
# Cache Management
# =============================================================================


def get_cache_path(repo_root: Path) -> Path:
    """Get path to issue state cache file."""
    cache_dir = repo_root / "state"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "issue-cache.json"


def load_cache(cache_path: Path) -> Dict[str, Any]:
    """Load existing cache or return empty dict."""
    if cache_path.exists():
        try:
            with open(cache_path) as f:
                data = json.load(f)
                return dict(data) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_cache(cache_path: Path, cache: Dict[str, Any]) -> None:
    """Save cache to file with atomic write for crash safety."""
    try:
        temp_path = cache_path.with_suffix(".tmp")
        with open(temp_path, "w") as f:
            json.dump(cache, f, indent=2)
        temp_path.rename(cache_path)
    except IOError as e:
        import sys

        print(f"Warning: Could not save cache: {e}", file=sys.stderr)


def extract_external_urls(task: TaskInfo) -> List[str]:
    """Extract external URLs from task's blocks, related, and tracking fields."""
    urls = []

    for field_name in ("tracking", "tracking_issue"):
        tracking = task.metadata.get(field_name)
        if tracking:
            if isinstance(tracking, list):
                for item in tracking:
                    if isinstance(item, str) and item.startswith("http"):
                        urls.append(item)
            elif isinstance(tracking, str) and tracking.startswith("http"):
                urls.append(tracking)

    blocks = task.metadata.get("blocks")
    if blocks:
        if isinstance(blocks, list):
            for item in blocks:
                if isinstance(item, str) and item.startswith("http"):
                    urls.append(item)
        elif isinstance(blocks, str) and blocks.startswith("http"):
            urls.append(blocks)

    related = task.metadata.get("related")
    if related:
        if isinstance(related, list):
            for item in related:
                if isinstance(item, str) and item.startswith("http"):
                    urls.append(item)
        elif isinstance(related, str) and related.startswith("http"):
            urls.append(related)

    return urls


def fetch_url_state(url: str) -> Optional[Dict[str, Any]]:
    """Fetch state for a GitHub/Linear URL."""
    gh_match = re.match(r"https://github\.com/([^/]+/[^/]+)/(issues|pull)/(\d+)", url)
    if gh_match:
        repo = gh_match.group(1)
        number = gh_match.group(3)
        state = fetch_github_issue_state(repo, number)
        if state:
            return {
                "state": state,
                "source": "github",
                "repo": repo,
                "number": number,
            }
        return None

    return None
