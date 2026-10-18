"""Denial snapshots are captured at the serialized claim decision."""

from pathlib import Path

import pytest
from gptme_coordination.db import CoordinationDB
from gptme_coordination.work import WorkClaimManager


@pytest.mark.parametrize("completed", [False, True])
def test_denial_snapshot(tmp_path: Path, completed: bool) -> None:
    with CoordinationDB(tmp_path / "coord.db") as db:
        work = WorkClaimManager(db, on_completed_check=lambda *_: False)
        held = work.claim("holder", "task")
        assert held is not None
        if completed:
            assert work.complete("holder", "task")
        before = work.get("task")
        assert work.claim("contender", "task") is None
        denial = work.last_denial
        assert denial is not None
        assert denial.reason == ("completed-check" if completed else "contention")
        assert denial.claimer == "contender"
        assert denial.prior == before
        assert work.get("task") == before
        assert not db.conn.in_transaction

        # A later successful attempt must not carry a stale denial.
        assert work.claim("contender", "other-task") is not None
        assert work.last_denial is None


def test_dry_run_denial_provides_snapshot_without_writes(tmp_path: Path) -> None:
    with CoordinationDB(tmp_path / "coord.db") as db:
        work = WorkClaimManager(db)
        work.claim("holder", "task")
        before = db.conn.total_changes
        assert work.claim("contender", "task", dry_run=True) is None
        assert work.last_denial is not None
        assert work.last_denial.reason == "contention"
        assert db.conn.total_changes == before
