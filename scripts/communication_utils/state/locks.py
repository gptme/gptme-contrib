"""
File-based locking for concurrent process coordination.

Provides thread-safe file locking to prevent race conditions
when multiple processes access shared resources.
"""

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path


class LockError(Exception):
    """Raised when lock acquisition fails."""

    pass


class FileLock:
    """
    File-based lock for coordinating concurrent processes.

    Uses fcntl advisory locking for Unix systems to prevent
    race conditions when multiple processes access shared files.
    """

    def __init__(self, lock_path: str | Path, timeout: float | None = None):
        """
        Initialize file lock.

        Args:
            lock_path: Path to lock file
            timeout: Maximum seconds to wait for lock (None = wait forever)
        """
        self.lock_path = Path(lock_path)
        self.timeout = timeout
        self._lock_file: int | None = None

    def acquire(self) -> bool:
        """
        Acquire the lock.

        Returns:
            True if lock acquired successfully

        Raises:
            LockError: If lock cannot be acquired within timeout
        """
        # Create lock file if it doesn't exist
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        start_time = time.time()

        while True:
            try:
                # Open file for writing (create if doesn't exist)
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC)

                # Try to acquire exclusive lock (non-blocking)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

                # Success! Save file descriptor
                self._lock_file = fd
                return True

            except OSError:
                # Lock is held by another process
                if self.timeout is None:
                    # Wait indefinitely
                    time.sleep(0.1)
                    continue

                # Check timeout
                elapsed = time.time() - start_time
                if elapsed >= self.timeout:
                    raise LockError(
                        f"Could not acquire lock on {self.lock_path} after {self.timeout}s"
                    )

                time.sleep(0.1)

    def release(self) -> None:
        """Release the lock."""
        if self._lock_file is not None:
            try:
                fcntl.flock(self._lock_file, fcntl.LOCK_UN)
                os.close(self._lock_file)
            finally:
                self._lock_file = None

    @contextmanager
    def locked(self):
        """
        Context manager for acquiring and releasing lock.

        Usage:
            with FileLock("/path/to/lock").locked():
                # Do work while holding lock
                pass
        """
        try:
            self.acquire()
            yield
        finally:
            self.release()

    def __enter__(self):
        """Enter context manager."""
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context manager."""
        self.release()

    def __del__(self):
        """Cleanup on deletion."""
        self.release()


@contextmanager
def file_lock(lock_path: str | Path, timeout: float | None = 10.0):
    """
    Convenience context manager for file locking.

    Args:
        lock_path: Path to lock file
        timeout: Maximum seconds to wait (None = wait forever)

    Usage:
        with file_lock("/tmp/mylock"):
            # Do work while holding lock
            pass
    """
    lock = FileLock(lock_path, timeout=timeout)
    try:
        lock.acquire()
        yield lock
    finally:
        lock.release()
