"""Concurrency regression tests for coordination DB initialization.

Two processes opening the same brand-new coordination database must not fail
with ``sqlite3.OperationalError: database is locked``. Changing a database to
WAL mode needs exclusive access to the file, and SQLite does **not** consult
the busy handler for that change: the loser of the race fails immediately,
even with ``PRAGMA busy_timeout`` already set. The conversion therefore has
to be retried explicitly.
"""

from __future__ import annotations

import multiprocessing
import sqlite3
import tempfile
from pathlib import Path

import pytest
from gptme_coordination import db as db_module
from gptme_coordination.db import CoordinationDB, _enable_wal


def _init_db(db_path: str, barrier: object) -> None:
    """Open the coordination DB, synchronized with the competing process."""
    barrier.wait(timeout=30)  # type: ignore[attr-defined]
    with CoordinationDB(db_path) as db:
        db.conn.execute("SELECT count(*) FROM sqlite_master").fetchone()


def test_two_processes_can_initialize_the_same_new_database() -> None:
    """Two processes racing to initialize one new database both succeed."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "coord.db")
        ctx = multiprocessing.get_context("fork")
        barrier = ctx.Barrier(2)
        procs = [
            ctx.Process(target=_init_db, args=(db_path, barrier)) for _ in range(2)
        ]
        try:
            for proc in procs:
                proc.start()
            for proc in procs:
                proc.join(timeout=60)
                assert (
                    proc.exitcode == 0
                ), f"process failed with exitcode {proc.exitcode}"
        finally:
            # An assertion or partial startup must not strand a non-daemon child.
            for proc in procs:
                if proc.is_alive():
                    proc.terminate()
            for proc in procs:
                if proc.pid is not None:
                    proc.join(timeout=5)
                    if proc.is_alive():
                        proc.kill()
                        proc.join(timeout=5)


def test_failed_worker_does_not_leave_its_peer_running(monkeypatch) -> None:
    """An early worker failure must also reap the peer waiting at the barrier."""
    ctx = multiprocessing.get_context("fork")
    ready = ctx.Event()
    worker_count = ctx.Value("i", 0)
    procs = []
    process = ctx.Process

    def fail_before_barrier(db_path: str, barrier: object) -> None:
        with worker_count.get_lock():
            worker_count.value += 1
            first = worker_count.value == 1
        if first:
            assert ready.wait(timeout=5)
            raise RuntimeError("injected failure before barrier")
        ready.set()
        barrier.wait()  # type: ignore[attr-defined]

    def track_process(*args, **kwargs):
        proc = process(*args, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(ctx, "Process", track_process)
    monkeypatch.setattr(__name__ + "._init_db", fail_before_barrier)
    try:
        with pytest.raises(AssertionError, match="process failed"):
            test_two_processes_can_initialize_the_same_new_database()
        assert len(procs) == 2
        assert all(not proc.is_alive() for proc in procs)
        assert all(proc.exitcode is not None for proc in procs)
    finally:
        # Keep this regression safe even when testing the broken cleanup.
        for proc in procs:
            if proc.is_alive():
                proc.kill()
            proc.join(timeout=5)


def test_wal_conversion_retries_until_reader_releases_lock(tmp_path, monkeypatch):
    """Force a real conversion failure, then release the reader on retry."""
    path = tmp_path / "reader.db"
    reader = sqlite3.connect(path, isolation_level=None)
    writer = sqlite3.connect(path, isolation_level=None)
    sleeps = []
    try:
        reader.execute("CREATE TABLE example (value INTEGER)")
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM example").fetchall()
        writer.execute("PRAGMA busy_timeout=0")

        def release_reader(delay):
            sleeps.append(delay)
            reader.execute("ROLLBACK")

        monkeypatch.setattr(db_module.time, "sleep", release_reader)
        _enable_wal(writer)
        assert sleeps == [db_module.WAL_RETRY_DELAY]
        assert writer.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    finally:
        reader.close()
        writer.close()


def test_wal_conversion_reraises_when_lock_outlasts_deadline(tmp_path, monkeypatch):
    """The retry budget is bounded and preserves SQLite's lock exception."""
    path = tmp_path / "locked.db"
    reader = sqlite3.connect(path, isolation_level=None)
    writer = sqlite3.connect(path, isolation_level=None)
    sleeps = []
    clock = iter([0.0, 0.0, 1.0])
    monkeypatch.setattr(db_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(db_module.time, "sleep", sleeps.append)
    try:
        reader.execute("CREATE TABLE example (value INTEGER)")
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM example").fetchall()
        writer.execute("PRAGMA busy_timeout=0")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            _enable_wal(writer, timeout=1.0)
        assert sleeps == [db_module.WAL_RETRY_DELAY]
    finally:
        reader.close()
        writer.close()


def test_wal_conversion_does_not_retry_unrelated_io_errors(monkeypatch):
    """A real storage failure must not be misclassified as lock contention."""
    from unittest.mock import Mock

    error = sqlite3.OperationalError("disk I/O error")
    conn = Mock(spec=sqlite3.Connection)
    conn.execute.side_effect = error
    sleep = Mock()
    monkeypatch.setattr(db_module.time, "sleep", sleep)
    with pytest.raises(sqlite3.OperationalError) as raised:
        _enable_wal(conn)
    assert raised.value is error
    conn.execute.assert_called_once_with("PRAGMA journal_mode=WAL")
    sleep.assert_not_called()
