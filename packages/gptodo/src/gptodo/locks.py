"""Task locking system for preventing concurrent task execution.

Locks are stored in state/locks/ directory (gitignored) to prevent
multiple agents/processes from working on the same task simultaneously.

Lock file format: {task_id}.lock containing JSON:
{
    "task_id": "task-name",
    "worker": "bob-session-123",
    "started": "2026-01-19T18:00:00Z",
    "timeout_hours": 4
}

Design doc: knowledge/technical-designs/task-state-improvements-design.md
Tracking: ErikBjare/bob#240 Phase 3
"""

import fcntl
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


# Default lock timeout in hours
DEFAULT_LOCK_TIMEOUT_HOURS = 4


@contextmanager
def _atomic_lock_file(path: Path, write: bool = False) -> Iterator[tuple[dict | None, Path]]:
    """Context manager for atomic file operations with flock.

    Uses fcntl.flock to ensure atomic read-modify-write operations,
    preventing race conditions when multiple processes access the same lock file.

    Args:
        path: Path to the lock file
        write: If True, prepare for writing (creates parent dirs)

    Yields:
        Tuple of (existing_data, path) where existing_data is the parsed JSON
        content of the file or None if file is empty/invalid.

    Note:
        The flock is automatically released when the context exits.
        For write operations, call path.write_text() or path.unlink() within
        the context to ensure atomicity.
    """
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)

    # Open file for read+write, creating if needed
    # Use 'a+' mode to create file if it doesn't exist without truncating
    fd = None
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)

        # Acquire exclusive lock (blocks until available)
        # Use LOCK_NB for non-blocking if we want to fail fast
        fcntl.flock(fd, fcntl.LOCK_EX)

        # Read existing content
        existing_data = None
        try:
            content = os.pread(fd, 10000, 0).decode("utf-8").strip()
            if content:
                existing_data = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

        yield existing_data, path

    finally:
        if fd is not None:
            # Release lock and close (flock released automatically on close)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


@dataclass
class TaskLock:
    """Represents a task lock."""

    task_id: str
    worker: str
    started: str  # ISO 8601 format
    timeout_hours: float = DEFAULT_LOCK_TIMEOUT_HOURS

    @classmethod
    def create(
        cls,
        task_id: str,
        worker: str,
        timeout_hours: float = DEFAULT_LOCK_TIMEOUT_HOURS,
    ) -> "TaskLock":
        """Create a new lock with current timestamp."""
        return cls(
            task_id=task_id,
            worker=worker,
            started=datetime.now(timezone.utc).isoformat(),
            timeout_hours=timeout_hours,
        )

    @classmethod
    def from_file(cls, path: Path) -> Optional["TaskLock"]:
        """Load lock from file, returns None if invalid or doesn't exist."""
        if not path.exists():
            return None
        try:
            with open(path) as f:
                data = json.load(f)
            return cls(**data)
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            logger.warning("Failed to parse lock file %s: %s", path, e)
            return None

    def to_file(self, path: Path) -> None:
        """Write lock to file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)

    def is_expired(self) -> bool:
        """Check if lock has exceeded its timeout."""
        started = datetime.fromisoformat(self.started)
        now = datetime.now(timezone.utc)
        elapsed_hours = (now - started).total_seconds() / 3600
        return elapsed_hours > self.timeout_hours

    def age_hours(self) -> float:
        """Get the age of the lock in hours."""
        started = datetime.fromisoformat(self.started)
        now = datetime.now(timezone.utc)
        return (now - started).total_seconds() / 3600


def get_locks_dir(repo_root: Path | None = None) -> Path:
    """Get the locks directory path.

    Uses TASKS_REPO_ROOT env var if set (for wrapper script),
    otherwise uses provided repo_root or current directory.
    """
    if repo_root is None:
        repo_root = Path(os.environ.get("TASKS_REPO_ROOT", "."))
    return repo_root / "state" / "locks"


def get_lock_path(task_id: str, repo_root: Path | None = None) -> Path:
    """Get the lock file path for a task."""
    # Sanitize task_id for use as filename (replace / with -)
    safe_id = task_id.replace("/", "-").replace("\\", "-")
    return get_locks_dir(repo_root) / f"{safe_id}.lock"


def acquire_lock(
    task_id: str,
    worker: str,
    timeout_hours: float = DEFAULT_LOCK_TIMEOUT_HOURS,
    repo_root: Path | None = None,
    force: bool = False,
) -> tuple[bool, TaskLock | None]:
    """Attempt to acquire a lock on a task.

    Uses fcntl.flock for atomic check-and-write operations, preventing race
    conditions when multiple processes attempt to acquire the same lock.

    Args:
        task_id: The task identifier
        worker: Worker identifier (e.g., "bob-session-123")
        timeout_hours: Lock timeout in hours (default 4)
        repo_root: Repository root path
        force: Force acquire even if existing lock (steals lock)

    Returns:
        (success, existing_lock): Tuple of:
            - success: True if lock was acquired, False if blocked
            - existing_lock: The previous lock holder if:
              - Lock was stolen via force=True (returns stolen lock)
              - Lock was taken over from expired holder (returns expired lock)
              - Blocked by another worker (returns blocking lock)
              Returns None if no previous lock existed or re-acquiring own lock.
    """
    lock_path = get_lock_path(task_id, repo_root)

    # Use atomic file operation with flock to prevent race conditions
    with _atomic_lock_file(lock_path, write=True) as (existing_data, path):
        # Parse existing lock if present
        existing: TaskLock | None = None
        if existing_data:
            try:
                existing = TaskLock(**existing_data)
            except (TypeError, KeyError) as e:
                logger.warning("Failed to parse lock data %s: %s", path, e)

        if existing is not None:
            # Check if same worker (re-acquiring own lock)
            if existing.worker == worker:
                # Update the lock (refresh timestamp)
                new_lock = TaskLock.create(task_id, worker, timeout_hours)
                _write_lock_atomic(path, new_lock)
                return True, None

            # Check if expired
            if existing.is_expired():
                # Expired lock - can take over
                new_lock = TaskLock.create(task_id, worker, timeout_hours)
                _write_lock_atomic(path, new_lock)
                return True, existing  # Return existing to show who had it

            # Valid lock held by another worker
            if force:
                # Force steal the lock
                new_lock = TaskLock.create(task_id, worker, timeout_hours)
                _write_lock_atomic(path, new_lock)
                return True, existing
            else:
                # Cannot acquire - blocked by another worker
                return False, existing

        # No existing lock - acquire it
        new_lock = TaskLock.create(task_id, worker, timeout_hours)
        _write_lock_atomic(path, new_lock)
        return True, None


def _write_lock_atomic(path: Path, lock: TaskLock) -> None:
    """Write lock data atomically (assumes caller holds flock)."""
    path.write_text(json.dumps(asdict(lock), indent=2))


def release_lock(
    task_id: str,
    worker: str,
    repo_root: Path | None = None,
    force: bool = False,
) -> tuple[bool, str | None]:
    """Release a lock on a task.

    Uses fcntl.flock for atomic check-and-delete operations.

    Args:
        task_id: The task identifier
        worker: Worker identifier
        repo_root: Repository root path
        force: Force release even if not owner

    Returns:
        (success, message): Tuple of success bool and message if failed
    """
    lock_path = get_lock_path(task_id, repo_root)

    if not lock_path.exists():
        return True, "No lock existed"

    # Use atomic file operation with flock
    with _atomic_lock_file(lock_path, write=False) as (existing_data, path):
        if existing_data is None:
            # Invalid lock file - remove it
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return True, "Removed invalid lock file"

        try:
            existing = TaskLock(**existing_data)
        except (TypeError, KeyError):
            # Invalid lock data - remove it
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return True, "Removed invalid lock file"

        # Check ownership
        if existing.worker != worker and not force:
            return False, f"Lock held by {existing.worker}, not {worker}"

        # Release the lock
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return True, None


def get_lock(task_id: str, repo_root: Path | None = None) -> TaskLock | None:
    """Get the current lock on a task, if any."""
    lock_path = get_lock_path(task_id, repo_root)
    return TaskLock.from_file(lock_path)


def list_locks(repo_root: Path | None = None) -> list[TaskLock]:
    """List all current locks."""
    locks_dir = get_locks_dir(repo_root)
    if not locks_dir.exists():
        return []

    locks = []
    for lock_file in locks_dir.glob("*.lock"):
        lock = TaskLock.from_file(lock_file)
        if lock is not None:
            locks.append(lock)
    return locks


def cleanup_expired_locks(repo_root: Path | None = None) -> list[TaskLock]:
    """Remove all expired locks.

    Returns:
        List of removed locks
    """
    locks_dir = get_locks_dir(repo_root)
    if not locks_dir.exists():
        return []

    removed = []
    for lock_file in locks_dir.glob("*.lock"):
        lock = TaskLock.from_file(lock_file)
        if lock is not None and lock.is_expired():
            try:
                lock_file.unlink()
                removed.append(lock)
            except OSError as e:
                logger.warning("Failed to remove expired lock %s: %s", lock_file, e)
    return removed


def is_task_locked(
    task_id: str, repo_root: Path | None = None, exclude_worker: str | None = None
) -> bool:
    """Check if a task is currently locked.

    Args:
        task_id: The task identifier
        repo_root: Repository root path
        exclude_worker: If provided, don't consider locks by this worker

    Returns:
        True if task is locked (by someone else if exclude_worker provided)
    """
    lock = get_lock(task_id, repo_root)
    if lock is None:
        return False
    if lock.is_expired():
        return False
    if exclude_worker and lock.worker == exclude_worker:
        return False
    return True
