"""Regressions for gptme-contrib#2320 (coordination defects)."""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from gptme_coordination import worktree_guard
from gptme_coordination.db import CoordinationDB
from gptme_coordination.messages import MessageBus
from gptme_coordination.work import WORK_SCHEMA, WorkClaimManager


@pytest.fixture
def db(tmp_path: Path) -> CoordinationDB:
    return CoordinationDB(tmp_path / "test.db")


def test_vacuum_ages_abandoned_rows_by_abandon_time(db: CoordinationDB) -> None:
    work = WorkClaimManager(db)
    work.claim("agent-a", "task")
    db.conn.execute("UPDATE work SET claimed_at = datetime('now', '-3 hours')")
    work.abandon("agent-a", "task", reason="blocked")

    work.vacuum_expired(abandoned_age_hours=1)
    claim = work.get("task")
    assert claim is not None and claim.result == "blocked"

    db.conn.execute("UPDATE work SET abandoned_at = datetime('now', '-2 hours')")
    work.vacuum_expired(abandoned_age_hours=1)
    assert work.get("task") is None


def test_vacuum_falls_back_to_claimed_at_for_old_rows(db: CoordinationDB) -> None:
    """Rows abandoned before abandoned_at existed are still swept."""
    work = WorkClaimManager(db)
    work.claim("agent-a", "task")
    work.abandon("agent-a", "task")
    db.conn.execute(
        "UPDATE work SET abandoned_at = NULL, claimed_at = datetime('now', '-3 hours')"
    )
    work.vacuum_expired(abandoned_age_hours=1)
    assert work.get("task") is None


def test_vacuum_ignores_abandon_time_left_by_an_earlier_claim(
    db: CoordinationDB,
) -> None:
    """An older package version re-abandons a task without stamping abandoned_at."""
    work = WorkClaimManager(db)
    work.claim("agent-a", "task")
    work.abandon("agent-a", "task")
    db.conn.execute("UPDATE work SET abandoned_at = datetime('now', '-3 hours')")
    work.claim("agent-b", "task")
    db.conn.execute("UPDATE work SET status = 'abandoned', claimer = NULL")

    work.vacuum_expired(abandoned_age_hours=1)
    assert work.get("task") is not None


def _create_work_table_without_abandoned_at(db: CoordinationDB) -> None:
    db.conn.execute(
        "CREATE TABLE work (task_id TEXT PRIMARY KEY, claimer TEXT, "
        "epoch INTEGER NOT NULL DEFAULT 0, claimed_at TEXT, expires_at TEXT, "
        "status TEXT NOT NULL DEFAULT 'available', result TEXT, metadata TEXT, "
        "completed_at TEXT, hmac TEXT)"
    )


def test_existing_database_gains_abandoned_at(db: CoordinationDB) -> None:
    _create_work_table_without_abandoned_at(db)
    work = WorkClaimManager(db)
    work.claim("agent-a", "task")
    assert work.abandon("agent-a", "task")


def test_locked_database_does_not_skip_the_migration(db: CoordinationDB) -> None:
    _create_work_table_without_abandoned_at(db)
    # The table's indexes already exist, so the ALTER is the first write.
    db.conn.executescript(WORK_SCHEMA)
    db.conn.execute("PRAGMA busy_timeout=0")
    other = sqlite3.connect(db.db_path, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            WorkClaimManager(db)
    finally:
        other.close()
    # Once the lock clears the migration runs, and a second pass is a no-op.
    WorkClaimManager(db)
    work = WorkClaimManager(db)
    work.claim("agent-a", "task")
    assert work.abandon("agent-a", "task")


@pytest.mark.parametrize("offset_hours", [2, -5])
def test_inbox_since_converts_aware_datetimes_to_utc(
    db: CoordinationDB, offset_hours: int
) -> None:
    bus = MessageBus(db)
    bus.send("a", "hi")
    local = datetime.now(timezone(timedelta(hours=offset_hours)))

    assert len(bus.inbox("b", since=local - timedelta(minutes=1))) == 1
    assert bus.inbox("b", since=local + timedelta(minutes=1)) == []


def test_push_guard_exempts_brain_repo_reached_by_symlink(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    brain.mkdir()
    link = tmp_path / "brain-link"
    link.symlink_to(brain)
    db_path = tmp_path / "coord.db"

    worktree_guard.run_push_guard(
        ["refs/heads/master abc123 refs/heads/master def456"],
        worktree_root=brain.resolve(),
        brain_root=link,
        remote_url="git@github.com:org/repo.git",
        session_id="sess-1",
        agent_id="agent-1",
        db_path=db_path,
    )

    assert not db_path.exists()


def test_occupancy_guard_runs_when_prefix_is_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS: /tmp is a symlink to /private/tmp."""
    real = tmp_path / "private" / "worktrees"
    git_dir = real / "pr" / "git"
    git_dir.mkdir(parents=True)
    (tmp_path / "tmp").symlink_to(tmp_path / "private")
    monkeypatch.setattr(
        worktree_guard, "_WORKTREE_PREFIX", f"{tmp_path}/tmp/worktrees/"
    )

    worktree_guard.run_guard(
        worktree_root=(real / "pr").resolve(),
        git_dir=git_dir,
        brain_root=tmp_path,
        session_id="sess-1",
        agent_id="agent-1",
        pid=os.getpid(),
    )

    ledger = tmp_path / worktree_guard.LEDGER_REL
    assert [json.loads(line) for line in ledger.read_text().splitlines()]
