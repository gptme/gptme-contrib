"""Tests for RunLoopLock."""

import os
import tempfile
import time
from pathlib import Path
from threading import Thread

from gptme_runloops.utils.lock import RunLoopLock


def test_lock_acquire_release():
    """Test basic lock acquisition and release."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lock = RunLoopLock(Path(tmpdir), "test")

        # Acquire lock
        assert lock.acquire()
        assert lock.lock_fd is not None

        # Release lock
        lock.release()
        assert lock.lock_fd is None


def test_lock_context_manager():
    """Test lock as context manager."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lock = RunLoopLock(Path(tmpdir), "test")

        with lock:
            assert lock.lock_fd is not None

        assert lock.lock_fd is None


def test_lock_no_wait_mode():
    """Test no-wait mode when lock is held."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lock1 = RunLoopLock(Path(tmpdir), "test")
        lock2 = RunLoopLock(Path(tmpdir), "test")

        # First lock acquires successfully
        assert lock1.acquire(wait=False)

        # Second lock fails immediately in no-wait mode
        assert not lock2.acquire(wait=False)

        # Cleanup
        lock1.release()


def test_lock_wait_mode():
    """Test wait mode with timeout."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lock1 = RunLoopLock(Path(tmpdir), "test")
        lock2 = RunLoopLock(Path(tmpdir), "test")

        # First lock acquires
        assert lock1.acquire()

        # Second lock waits but times out
        start = time.time()
        assert not lock2.acquire(wait=True, timeout=2)
        elapsed = time.time() - start

        # Should have waited close to timeout
        assert 1.5 < elapsed < 2.5

        # Cleanup
        lock1.release()


def test_lock_concurrent_access():
    """Test concurrent access from multiple threads."""
    with tempfile.TemporaryDirectory() as tmpdir:
        results = []

        def try_lock():
            lock = RunLoopLock(Path(tmpdir), "test")
            success = lock.acquire(wait=False)
            results.append(success)
            if success:
                time.sleep(0.1)  # Hold briefly
                lock.release()

        # Start 5 threads trying to acquire same lock
        threads = [Thread(target=try_lock) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Only one should have succeeded
        assert sum(results) == 1


def test_lock_pid_written():
    """Test that PID is written to lock file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lock = RunLoopLock(Path(tmpdir), "test")
        lock.acquire()

        # Read lock file
        content = lock.lock_file.read_text()
        assert str(os.getpid()) in content

        lock.release()
