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

from gptme_coordination.db import CoordinationDB


def _init_db(db_path: str, barrier: object) -> None:
    """Open the coordination DB, synchronized with the competing process."""
    # Open a plain connection first so both processes race on the *same*
    # brand-new file, then line up before the initialization runs.
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.close()
    barrier.wait()  # type: ignore[attr-defined]
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
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(timeout=60)
            assert proc.exitcode == 0, f"process failed with exitcode {proc.exitcode}"
