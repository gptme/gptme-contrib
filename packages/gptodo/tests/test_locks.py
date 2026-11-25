"""Concurrency tests for gptodo task locks (gptme/gptme-contrib#2304 item 6)."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone


import gptodo.locks as locks
from gptodo.locks import (
    TaskLock,
    _atomic_lock_file,
    cleanup_expired_locks,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_atomic_lock_file_retries_on_stale_inode(tmp_path, monkeypatch):
    """If the inode locked is not the one currently at the path (a concurrent
    unlink+recreate while blocked on flock), the acquirer must retry on the
    fresh file rather than proceed holding the unlinked inode."""
    path = tmp_path / "t.lock"
    calls = {"n": 0}
    real = locks._same_inode

    def fake(fd, p):
        calls["n"] += 1
        return calls["n"] > 1  # first attempt: stale; retry: fresh

    monkeypatch.setattr(locks, "_same_inode", fake)
    with _atomic_lock_file(path, write=True) as (_data, _p):
        pass
    assert calls["n"] == 2, "did not retry after locking a stale inode"
    assert real is not None


def test_cleanup_respects_concurrent_takeover(tmp_path):
    """cleanup_expired_locks must take the flock so it cannot unlink a lock a
    concurrent acquirer is taking over (which yields two holders)."""
    repo_root = tmp_path
    locks_dir = repo_root / "state" / "locks"
    locks_dir.mkdir(parents=True)
    lock_path = locks_dir / "t.lock"

    expired = TaskLock(
        task_id="t",
        worker="A",
        started=(_now() - timedelta(hours=10)).isoformat(),
        timeout_hours=1,
    )
    lock_path.write_text(json.dumps(asdict(expired)))

    # Simulate acquirer B: it holds the flock while it takes over.
    fd = os.open(str(lock_path), os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)

    done = threading.Event()
    removed: dict[str, object] = {}

    def worker() -> None:
        removed["list"] = cleanup_expired_locks(repo_root)
        done.set()

    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.3)
    # Without the flock, cleanup unlinks immediately and finishes.
    assert not done.is_set(), "cleanup unlinked a lock held under flock (race)"
    assert lock_path.exists()

    # B completes the takeover: write a fresh, non-expired lock and release.
    fresh = TaskLock(
        task_id="t",
        worker="B",
        started=_now().isoformat(),
        timeout_hours=1,
    )
    payload = json.dumps(asdict(fresh)).encode()
    os.pwrite(fd, payload, 0)
    os.ftruncate(fd, len(payload))
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)

    assert done.wait(5), "cleanup never completed after the lock was released"
    assert lock_path.exists(), "cleanup removed a freshly-renewed lock"
    assert removed["list"] == []
