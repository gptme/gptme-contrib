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
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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


# Deprecated state aliases - these map to their canonical state
# Used by normalize_state() to provide backward compatibility
DEPRECATED_STATE_ALIASES: dict[str, str] = {
    "new": "backlog",  # new → backlog (untriaged work)
    "someday": "backlog",  # someday → backlog (deferred work)
    "paused": "backlog",  # paused → backlog (intentionally deferred)
}


def normalize_state(state: str, warn: bool = True) -> str:
    """Normalize deprecated state aliases to their canonical form.

    Args:
        state: The state string to normalize
        warn: If True, emit deprecation warning for deprecated states

    Returns:
        The canonical state (or original if already canonical/unknown)

    Examples:
        >>> normalize_state("new")
        'backlog'
        >>> normalize_state("active")
        'active'
    """
    import warnings

    if state in DEPRECATED_STATE_ALIASES:
        canonical = DEPRECATED_STATE_ALIASES[state]
        if warn:
            warnings.warn(
                f"State '{state}' is deprecated, use '{canonical}' instead. "
                f"Deprecated states will be removed in a future version.",
                DeprecationWarning,
                stacklevel=2,
            )
        return canonical
    return state


def get_canonical_states() -> list[str]:
    """Get list of canonical (non-deprecated) task states."""
    return ["backlog", "todo", "active", "waiting", "done", "cancelled"]


CONFIGS = {
    "tasks": DirectoryConfig(
        type_name="tasks",
        # New state model per Issue #240 design:
        # - backlog: not triaged or intentionally deferred (consolidates new/someday/paused)
        # - todo: triaged and ready to pick up
        # - active: being actively worked on
        # - waiting: blocked on external response
        # - done: completed
        # - cancelled: won't do
        # Also accepts deprecated aliases: new, someday, paused (with warnings)
        states=[
            "backlog",
            "todo",
            "active",
            "waiting",
            "done",
            "cancelled",
            # Deprecated aliases accepted for backward compatibility
            "new",
            "someday",
            "paused",
        ],
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
    # Tasks - canonical states
    "backlog": ("yellow", "backlog"),
    "todo": ("cyan", "todo"),
    "active": ("blue", "active"),
    "waiting": ("magenta", "waiting"),
    "done": ("green", "done"),
    "cancelled": ("red", "cancelled"),
    # Deprecated task states (still accepted, mapped to canonical)
    "new": ("yellow", "new"),  # deprecated → backlog
    "paused": ("cyan", "paused"),  # deprecated → backlog
    "someday": ("yellow", "someday"),  # deprecated → backlog
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
    # Canonical task states
    "backlog": "📥",
    "todo": "📋",
    "active": "🏃",
    "waiting": "⏳",
    "done": "✅",
    "cancelled": "❌",
    # Deprecated task states (still accepted)
    "new": "🆕",  # deprecated → backlog
    "paused": "⚪",  # deprecated → backlog
    "someday": "💭",  # deprecated → backlog
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
    """Information about a task with metadata and validation.

    This class represents a task file with its metadata, content analysis,
    and validation status. It provides a unified interface for accessing
    task information across the codebase.

    Attributes:
        path: Path to the task file
        name: Filename without .md extension
        state: Current state from frontmatter (backlog, todo, active, waiting, done, cancelled)
        created: Creation timestamp
        modified: Last modification timestamp
        priority: Task priority (high, medium, low)
        tags: List of tags
        depends: List of task dependencies (deprecated, use requires instead)
        requires: List of required task IDs or URLs (canonical for depends)
        related: List of related task IDs or URLs
        parent: Parent task ID or URL (for subtasks)
        discovered_from: List of task IDs this was discovered from
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
    depends: List[str]  # Deprecated, use requires instead
    requires: List[str]  # Required dependencies (task IDs or URLs)
    related: List[str]  # Related items (informational)
    parent: Optional[str]  # Parent task ID
    discovered_from: List[str]  # Tasks this was discovered from
    subtasks: SubtaskCount
    issues: List[str]
    metadata: Dict
    # Multi-agent coordination fields (Phase 2)
    parallelizable: bool = False  # Can run concurrently with other work
    isolation: Optional[str] = None  # none, worktree, container
    worktree_path: Optional[str] = None  # If using worktree isolation
    assigned_to: Optional[str] = None  # Which agent instance owns this
    assigned_at: Optional[datetime] = None  # When assignment started
    lock_timeout_hours: Optional[int] = None  # Override default lock timeout
    spawned_from: Optional[str] = None  # Parent task that spawned this
    spawned_tasks: List[str] = field(default_factory=list)  # Child tasks
    coordination_mode: Optional[str] = None  # sequential, parallel, fan-out-fan-in

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


# =============================================================================
# Basic Utility Functions
# =============================================================================


def find_repo_root(start_path: Path) -> Path:
    """Find the repository root by looking for .git directory.

    If TASKS_REPO_ROOT environment variable is set, uses that as the
    starting point instead. This allows the wrapper script to run in
    a different directory (for module discovery) while still finding
    tasks in the original workspace.
    """
    import os

    # Check for explicit repo root from wrapper script
    if env_root := os.environ.get("TASKS_REPO_ROOT"):
        start_path = Path(env_root)

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


def has_new_activity(updated_at: Optional[str], waiting_since: Optional[str | date]) -> bool:
    """Check if there's been new activity since waiting_since.

    Args:
        updated_at: ISO format timestamp from GitHub/Linear (e.g., "2026-01-12T10:30:00Z")
        waiting_since: Date when task started waiting (from task frontmatter)

    Returns:
        True if updated_at is after waiting_since, False otherwise.
        Returns False if either value is None.
    """
    if not updated_at or not waiting_since:
        return False

    try:
        # Parse updated_at (ISO format with timezone)
        if isinstance(updated_at, str):
            # Handle both "2026-01-12T10:30:00Z" and "2026-01-12" formats
            if "T" in updated_at:
                updated_dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            else:
                updated_dt = datetime.fromisoformat(updated_at)
        else:
            return False

        # Parse waiting_since (could be date or datetime string)
        if isinstance(waiting_since, date) and not isinstance(waiting_since, datetime):
            # Convert date to datetime at start of day
            waiting_dt = datetime.combine(waiting_since, datetime.min.time())
        elif isinstance(waiting_since, str):
            if "T" in waiting_since:
                waiting_dt = datetime.fromisoformat(waiting_since.replace("Z", "+00:00"))
            else:
                waiting_dt = datetime.fromisoformat(waiting_since)
        else:
            return False

        # Make both timezone-naive for comparison if needed.
        # Use astimezone() to convert to local time BEFORE stripping tzinfo,
        # otherwise we'd compare UTC times against local midnight incorrectly.
        if updated_dt.tzinfo and not waiting_dt.tzinfo:
            updated_dt = updated_dt.astimezone().replace(tzinfo=None)
        elif waiting_dt.tzinfo and not updated_dt.tzinfo:
            waiting_dt = waiting_dt.astimezone().replace(tzinfo=None)

        return updated_dt > waiting_dt
    except (ValueError, TypeError):
        return False


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


# =============================================================================
# Task Validation and Loading
# =============================================================================


def validate_task_file(file: Path, post: "fm.Post") -> List[str]:
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

    for field_name, expected_type in required_fields.items():
        if field_name not in metadata:
            issues.append(f"Missing required field: {field_name}")
        elif isinstance(expected_type, tuple):
            if not isinstance(metadata[field_name], expected_type):
                type_names = " or ".join(t.__name__ for t in expected_type)
                issues.append(f"Field {field_name} must be {type_names}")
        elif not isinstance(metadata[field_name], expected_type):
            issues.append(f"Field {field_name} must be {expected_type.__name__}")

    # Validate state value
    if "state" in metadata:
        state = metadata["state"]
        # Normalize deprecated states before validation
        normalized_state = normalize_state(state, warn=False)
        # Use canonical states only (not deprecated ones in CONFIGS)
        canonical_states = get_canonical_states()
        if normalized_state not in canonical_states:
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
                issues.append("Created date must be ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")

    # Optional field validation
    if "priority" in metadata:
        priority = metadata["priority"]
        if priority not in ("high", "medium", "low", None):
            issues.append("Priority must be 'high', 'medium', or 'low'")

    if "tags" in metadata and not isinstance(metadata["tags"], list):
        issues.append("Tags must be a list")

    if "depends" in metadata and not isinstance(metadata["depends"], list):
        issues.append("Dependencies must be a list")

    if "requires" in metadata and not isinstance(metadata["requires"], list):
        issues.append("Requires must be a list")

    # Also check deprecated blocks field
    if "blocks" in metadata and not isinstance(metadata["blocks"], list):
        issues.append("Blocks must be a list")

    if "related" in metadata and not isinstance(metadata["related"], list):
        issues.append("Related must be a list")

    if "discovered-from" in metadata and not isinstance(metadata["discovered-from"], list):
        issues.append("Discovered-from must be a list")

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
    """Load tasks from directory or single file with metadata.

    Args:
        tasks_dir: Directory containing task files
        recursive: Whether to search subdirectories
        single_file: Optional specific file to load

    Returns:
        List of TaskInfo objects
    """
    frontmatter = _get_frontmatter()
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

            # Get state (default to backlog if missing)
            state = metadata.get("state")
            if not state:
                issues.append("No state in frontmatter")
                state = "backlog"  # Default state (canonical)
            else:
                # Normalize deprecated states (new/someday/paused → backlog)
                # Note: warnings suppressed during load, validated separately
                state = normalize_state(state, warn=False)

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
            # Get relationship fields (new typed dependencies)
            # requires is canonical, depends is deprecated alias, blocks is NOT merged (different semantics)
            depends_list = metadata.get("depends", [])
            blocks_list = metadata.get("blocks", [])  # NOT merged - different semantics
            requires_list = metadata.get("requires", [])

            # Warn if multiple fields are present (potential confusion)
            fields_present = []
            if requires_list:
                fields_present.append("requires")
            if blocks_list:
                fields_present.append("blocks")
            if depends_list:
                fields_present.append("depends")
            if len(fields_present) > 1:
                warnings.warn(
                    f"Task '{file.stem}' has multiple dependency fields: {fields_present}. "
                    f"Using 'requires' (canonical). Note: 'blocks' has different semantics and is ignored.",
                    DeprecationWarning,
                    stacklevel=2,
                )

            # Merge depends into requires (depends is deprecated alias with same semantics)
            # blocks is NOT merged because it has inverse semantics (this task blocks X, not X blocks this)
            # Priority: requires > depends (blocks is separate)
            effective_requires = requires_list if requires_list else depends_list

            # Parse assigned_at timestamp if present
            assigned_at = None
            if metadata.get("assigned_at"):
                try:
                    assigned_at = parse_datetime_field(metadata.get("assigned_at"))
                    if assigned_at and assigned_at.tzinfo:
                        assigned_at = assigned_at.astimezone().replace(tzinfo=None)
                except (ValueError, TypeError):
                    pass  # Leave as None if parsing fails

            task = TaskInfo(
                path=file,
                name=file.stem,
                state=state,
                created=created,
                modified=modified,
                priority=metadata.get("priority"),
                tags=metadata.get("tags", []),
                depends=depends_list,  # Deprecated, use requires instead
                requires=effective_requires,  # Canonical required deps
                related=metadata.get("related", []),
                parent=metadata.get("parent"),
                discovered_from=metadata.get("discovered-from", []),
                subtasks=subtasks,
                issues=issues,
                metadata=metadata,
                # Multi-agent coordination fields (Phase 2)
                parallelizable=bool(metadata.get("parallelizable", False)),
                isolation=metadata.get("isolation"),
                worktree_path=metadata.get("worktree_path"),
                assigned_to=metadata.get("assigned_to"),
                assigned_at=assigned_at,
                lock_timeout_hours=metadata.get("lock_timeout_hours"),
                spawned_from=metadata.get("spawned_from"),
                spawned_tasks=metadata.get("spawned_tasks", []),
                coordination_mode=metadata.get("coordination_mode"),
            )
            tasks.append(task)

        except Exception as e:
            logging.error(f"Error reading {file}: {e}")

    return tasks


# =============================================================================
# Task Resolution and Ready Check
# =============================================================================


def task_to_dict(task: TaskInfo) -> Dict[str, Any]:
    """Serialize TaskInfo to a JSON-compatible dictionary.

    Returns dict with:
    - id: task filename without .md
    - state: current state
    - priority: task priority
    - created: ISO timestamp
    - modified: ISO timestamp
    - tags: list of tags
    - requires: list of required dependencies (canonical)
    - related: list of related items
    - parent: parent task ID
    - discovered_from: list of tasks this was discovered from
    - depends: list of dependencies (deprecated, same as requires)
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
        "requires": task.requires,  # Canonical required deps
        "related": task.related,
        "parent": task.parent,
        "discovered_from": task.discovered_from,
        "depends": task.depends,  # Deprecated, kept for compatibility
        "subtasks": {
            "completed": task.subtasks.completed,
            "total": task.subtasks.total,
        },
        "has_issues": task.has_issues,
        # Multi-agent coordination fields
        "parallelizable": task.parallelizable,
        "isolation": task.isolation,
        "worktree_path": task.worktree_path,
        "assigned_to": task.assigned_to,
        "assigned_at": task.assigned_at.isoformat() if task.assigned_at else None,
        "lock_timeout_hours": task.lock_timeout_hours,
        "spawned_from": task.spawned_from,
        "spawned_tasks": task.spawned_tasks,
        "coordination_mode": task.coordination_mode,
    }


def is_task_ready(
    task: TaskInfo,
    all_tasks: Dict[str, TaskInfo],
    issue_cache: Optional[Dict[str, Any]] = None,
) -> bool:
    """Check if a task is ready (unblocked) to work on.

    A task is ready if:
    - It has no required dependencies, OR
    - All its required dependencies are in "done" or "cancelled" state
    - All URL-based requires are CLOSED (if cache provided)

    Uses task.requires (canonical) which includes both explicit requires
    and deprecated depends/blocks entries.

    Args:
        task: Task to check
        all_tasks: Dictionary mapping task names to TaskInfo objects
        issue_cache: Optional cache of issue states for URL-based requires

    Returns:
        True if task is ready, False if blocked
    """
    # Use requires (canonical field, includes deprecated depends/blocks)
    requires = task.requires
    if not requires:
        return True

    # Separate URL-based and task-based requires
    url_requires = []
    task_requires = []
    for req in requires:
        if isinstance(req, str) and req.startswith("http"):
            url_requires.append(req)
        else:
            task_requires.append(req)

    # Check task-based blocking dependencies
    for dep_name in task_requires:
        dep_task = all_tasks.get(dep_name)
        if dep_task is None:
            # Missing dependency = blocked (should be validated separately)
            return False
        if dep_task.state not in ["done", "cancelled"]:
            # Dependency not completed = blocked
            return False

    # Check URL-based requires if cache provided
    if issue_cache and url_requires:
        for req_url in url_requires:
            cached = issue_cache.get(req_url)
            if cached:
                # If URL is OPEN, task is blocked
                if cached.get("state") == "OPEN":
                    return False
            # If not in cache, we can't determine - assume not blocked

    # All required dependencies resolved = ready
    return True


def compute_effective_state(
    task: TaskInfo,
    all_tasks: Dict[str, TaskInfo],
    issue_cache: Optional[Dict[str, Any]] = None,
) -> str:
    """Compute the effective state of a task including virtual 'blocked' state.

    Effective state considers both the task's actual state AND its dependencies:
    - If task state is 'done' or 'cancelled' → returns that state (terminal)
    - If ANY required dependency (task or URL) is not resolved → returns 'blocked'
    - Otherwise → returns the task's actual state

    'blocked' is a virtual state that is never written to frontmatter.
    It indicates the task cannot be worked on until dependencies resolve.

    Args:
        task: Task to compute effective state for
        all_tasks: Dictionary mapping task names to TaskInfo objects
        issue_cache: Optional cache of issue states for URL-based requires

    Returns:
        Effective state string (including virtual 'blocked' state)
    """
    # Terminal states are always returned as-is
    if task.state in ("done", "cancelled"):
        return task.state or "unknown"

    # Check if task is blocked by dependencies
    requires = task.requires
    if not requires:
        # No dependencies = actual state
        return task.state or "unknown"

    # Check each dependency
    for req in requires:
        if isinstance(req, str) and req.startswith("http"):
            # URL-based dependency - check cache
            if issue_cache:
                cached = issue_cache.get(req)
                if cached:
                    # If URL is OPEN, task is blocked
                    if cached.get("state") == "OPEN":
                        return "blocked"
                # If not in cache, assume not blocked (can't determine)
            # Without cache, assume not blocked
        else:
            # Task-based dependency
            dep_task = all_tasks.get(req)
            if dep_task is None:
                # Missing dependency = blocked
                return "blocked"
            if dep_task.state not in ("done", "cancelled"):
                # Dependency not completed = blocked
                return "blocked"

    # All dependencies resolved = actual state
    return task.state or "unknown"


def get_blocking_reasons(
    task: TaskInfo,
    all_tasks: Dict[str, TaskInfo],
    issue_cache: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Get list of reasons why a task is blocked.

    Returns empty list if task is not blocked.

    Args:
        task: Task to check
        all_tasks: Dictionary mapping task names to TaskInfo objects
        issue_cache: Optional cache of issue states

    Returns:
        List of blocking reason strings
    """
    # Terminal states are never blocked
    if task.state in ("done", "cancelled"):
        return []

    requires = task.requires
    if not requires:
        return []

    reasons = []
    for req in requires:
        if isinstance(req, str) and req.startswith("http"):
            # URL-based dependency
            if issue_cache:
                cached = issue_cache.get(req)
                if cached and cached.get("state") == "OPEN":
                    reasons.append(f"Waiting on: {req}")
            # Without cache, can't determine
        else:
            # Task-based dependency
            dep_task = all_tasks.get(req)
            if dep_task is None:
                reasons.append(f"Missing task: {req}")
            elif dep_task.state not in ("done", "cancelled"):
                reasons.append(f"Blocked by: {req} ({dep_task.state})")

    return reasons


def resolve_tasks(task_ids: List[str], tasks: List[TaskInfo], tasks_dir: Path) -> List[TaskInfo]:
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


# =============================================================================
# GitHub API Helpers
# =============================================================================


def parse_tracking_ref(ref: str) -> Optional[Dict[str, str]]:
    """Parse tracking reference to extract repo and issue number.

    Supports formats:
    - owner/repo#123
    - https://github.com/owner/repo/issues/123
    - https://github.com/owner/repo/pull/123
    - https://linear.app/TEAM/issue/IDENTIFIER/...
    """
    # Full GitHub URL format
    url_match = re.match(r"https://github\.com/([^/]+/[^/]+)/(issues|pull)/(\d+)", ref)
    if url_match:
        return {
            "repo": url_match.group(1),
            "number": url_match.group(3),
            "source": "github",
        }

    # Linear URL format
    linear_match = re.match(r"https://linear\.app/([^/]+)/issue/([^/]+)", ref)
    if linear_match:
        return {
            "team": linear_match.group(1),
            "identifier": linear_match.group(2),
            "source": "linear",
        }

    # Short format: owner/repo#123
    short_match = re.match(r"([^/]+/[^#]+)#(\d+)", ref)
    if short_match:
        return {
            "repo": short_match.group(1),
            "number": short_match.group(2),
            "source": "github",
        }

    return None


def fetch_github_issue_state(repo: str, number: str) -> Optional[str]:
    """Fetch GitHub issue/PR state using gh CLI.

    Note: For state + updatedAt, use fetch_github_issue_details() instead.
    """
    details = fetch_github_issue_details(repo, number)
    if details:
        return details.get("state")
    return None


def fetch_github_issue_details(repo: str, number: str) -> Optional[Dict[str, Any]]:
    """Fetch GitHub issue/PR state and metadata including updatedAt.

    Returns:
        Dict with 'state' and 'updatedAt' fields, or None on failure.
        updatedAt is ISO format string: "2026-01-12T10:30:00Z"
    """
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
                "state,updatedAt",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return {
                "state": data.get("state"),
                "updatedAt": data.get("updatedAt"),
            }
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
                "state,updatedAt",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return {
                "state": data.get("state"),
                "updatedAt": data.get("updatedAt"),
            }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
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
    import urllib.request

    token = os.environ.get("LINEAR_API_KEY")
    if not token:
        return None

    # GraphQL query to get issue state by identifier
    query = """
    query($filter: IssueFilter) {
        issues(filter: $filter, first: 1) {
            nodes {
                state { type }
            }
        }
    }
    """

    variables = {"filter": {"identifier": {"eq": identifier}}}

    try:
        req = urllib.request.Request(
            "https://api.linear.app/graphql",
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": token,
            },
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())

        nodes = data.get("data", {}).get("issues", {}).get("nodes", [])
        if nodes and nodes[0].get("state"):
            state_type: str = nodes[0]["state"]["type"]
            return state_type
    except Exception:
        pass
    return None


def update_task_state(task_path: Path, new_state: str) -> bool:
    """Update task frontmatter state field.

    If new_state is a deprecated alias (new, someday, paused),
    it will be normalized to the canonical state (backlog) with a warning.
    """
    frontmatter = _get_frontmatter()
    try:
        # Normalize deprecated states with warning
        canonical_state = normalize_state(new_state, warn=True)
        post = frontmatter.load(task_path)
        post["state"] = canonical_state
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
        # Write to temp file first, then rename for atomicity
        temp_path = cache_path.with_suffix(".tmp")
        with open(temp_path, "w") as f:
            json.dump(cache, f, indent=2)
        temp_path.rename(cache_path)
    except IOError as e:
        # Log but don't crash - cache is non-critical
        import sys

        print(f"Warning: Could not save cache: {e}", file=sys.stderr)


def extract_external_urls(task: TaskInfo) -> List[str]:
    """Extract external URLs from task's requires, related, and tracking fields."""
    urls = []

    # Check tracking field (full URLs)
    tracking = task.metadata.get("tracking")
    if tracking:
        if isinstance(tracking, list):
            for item in tracking:
                if isinstance(item, str) and item.startswith("http"):
                    urls.append(item)
        elif isinstance(tracking, str) and tracking.startswith("http"):
            urls.append(tracking)

    # Check blocks field
    blocks = task.metadata.get("blocks")
    if blocks:
        if isinstance(blocks, list):
            for item in blocks:
                if isinstance(item, str) and item.startswith("http"):
                    urls.append(item)
        elif isinstance(blocks, str) and blocks.startswith("http"):
            urls.append(blocks)

    # Check related field
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
    """Fetch state and metadata for a GitHub/Linear URL.

    Returns:
        Dict with 'state', 'source', 'repo'/'team', 'number'/'identifier',
        and optionally 'updatedAt' for activity tracking.
    """
    # Parse GitHub URL
    gh_match = re.match(r"https://github\.com/([^/]+/[^/]+)/(issues|pull)/(\d+)", url)
    if gh_match:
        repo = gh_match.group(1)
        number = gh_match.group(3)
        details = fetch_github_issue_details(repo, number)
        if details:
            return {
                "state": details.get("state"),
                "updatedAt": details.get("updatedAt"),
                "source": "github",
                "repo": repo,
                "number": number,
            }
        return None

    # Parse Linear URL
    linear_match = re.match(r"https://linear\.app/([^/]+)/issue/([^/]+)", url)
    if linear_match:
        team = linear_match.group(1)
        identifier = linear_match.group(2)
        raw_state = fetch_linear_issue_state(identifier)
        if raw_state:
            # Normalize Linear states to OPEN/CLOSED format
            # Linear states like "completed", "canceled" → CLOSED
            # Other states like "started", "backlog", "in_progress" → OPEN
            normalized_state = (
                "CLOSED" if raw_state.lower() in ("completed", "canceled", "done") else "OPEN"
            )
            return {
                "state": normalized_state,
                "source": "linear",
                "team": team,
                "identifier": identifier,
            }
        return None

    return None
