"""File-based locking for run loops to prevent concurrent execution."""

import fcntl
import os
import time
from pathlib import Path


class RunLoopLock:
    """File-based lock for preventing concurrent run loop execution.

    Uses fcntl for POSIX file locking. Supports both no-wait (try once)
    and wait (retry with timeout) modes.

    Writes history to /tmp/gptme-lock-history.log for calendar generation.
    """

    HISTORY_FILE = Path("/tmp/gptme-lock-history.log")

    def __init__(self, lock_dir: Path, lock_name: str):
        """Initialize lock.

        Args:
            lock_dir: Directory for lock files
            lock_name: Name of this lock (e.g., "autonomous", "email")
        """
        self.lock_dir = Path(lock_dir)
        self.lock_name = lock_name
        self.lock_file = self.lock_dir / f"gptme-{lock_name}.lock"
        self.lock_fd: int | None = None
        self._work_description: str | None = None  # Description of work for calendar

        # Ensure lock directory exists
        self.lock_dir.mkdir(parents=True, exist_ok=True)

    def set_work_description(self, description: str) -> None:
        """Set work description for calendar entry.

        Args:
            description: Brief description of work being done
        """
        # Sanitize: remove pipes and newlines which break log format
        self._work_description = description.replace("|", "-").replace("\n", " ")[:200]

    def _log_history(self, action: str) -> None:
        """Log lock action to history file.

        Format: timestamp|ACTION|PID|lock_type|script_name|description
        Extended from old format with optional description field.

        Args:
            action: "ACQUIRED" or "RELEASED"
        """
        try:
            timestamp = int(time.time())
            pid = os.getpid()
            # Try to get script name from process
            try:
                with open(f"/proc/{pid}/cmdline") as f:
                    cmdline = f.read().split("\0")
                    script_name = Path(cmdline[0]).name if cmdline else "python"
            except Exception:
                script_name = "python"

            # Add description if available (6th field)
            description = self._work_description or ""
            log_entry = f"{timestamp}|{action}|{pid}|{self.lock_name}|{script_name}|{description}\n"

            # Append to history file
            with open(self.HISTORY_FILE, "a") as f:
                f.write(log_entry)
        except Exception as e:
            # Don't fail lock operation if history logging fails
            print(f"Warning: Failed to log lock history: {e}")

    def acquire(self, wait: bool = False, timeout: int = 60) -> bool:
        """Acquire the lock.

        Args:
            wait: If True, retry for up to timeout seconds. If False, try once.
            timeout: Maximum seconds to wait for lock (only if wait=True)

        Returns:
            True if lock acquired, False otherwise
        """
        if self.lock_fd is not None:
            # Already have lock
            return True

        # Open lock file (create if doesn't exist)
        fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o644)

        start_time = time.time()
        while True:
            try:
                # Try to acquire lock (non-blocking)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

                # Success! Write PID to lock file
                os.ftruncate(fd, 0)
                os.write(fd, f"{os.getpid()}\n".encode())
                os.fsync(fd)

                self.lock_fd = fd

                # Log to history for calendar generation
                self._log_history("ACQUIRED")

                return True

            except BlockingIOError:
                # Lock is held by another process
                if not wait:
                    # No-wait mode: fail immediately
                    os.close(fd)
                    return False

                # Wait mode: check timeout
                elapsed = time.time() - start_time
                if elapsed >= timeout:
                    # Timeout exceeded
                    os.close(fd)
                    return False

                # Sleep briefly before retry
                time.sleep(0.5)

    def release(self) -> None:
        """Release the lock."""
        if self.lock_fd is None:
            return

        try:
            # Log to history before releasing
            self._log_history("RELEASED")

            # Release lock and close file descriptor
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            os.close(self.lock_fd)
        finally:
            self.lock_fd = None

    def __enter__(self) -> "RunLoopLock":
        """Context manager entry."""
        if not self.acquire():
            raise RuntimeError(f"Failed to acquire lock: {self.lock_name}")
        return self

    def __exit__(self, *args) -> None:
        """Context manager exit."""
        self.release()

    def __del__(self) -> None:
        """Cleanup on garbage collection."""
        self.release()
