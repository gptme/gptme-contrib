"""Workspace admission policies run under the same write lock as claim CAS."""

import multiprocessing
from pathlib import Path

import pytest
from gptme_coordination.db import CoordinationDB
from gptme_coordination.work import WorkClaimDenial, WorkClaimManager


class LinkedWork(WorkClaimManager):
    def _claim_admission(self, agent_id: str, task_id: str) -> WorkClaimDenial | None:
        assert self.db.conn.in_transaction
        for key in ("task", "issue"):
            prior = self.get(key)
            if prior and prior.status == "claimed" and prior.claimer != agent_id:
                return WorkClaimDenial("linked-contention", agent_id, prior)
        return None


@pytest.mark.parametrize("first,second", [("task", "issue"), ("issue", "task")])
def test_linked_admission_and_same_owner(
    tmp_path: Path, first: str, second: str
) -> None:
    with CoordinationDB(tmp_path / "coord.db") as db:
        work = LinkedWork(db)
        assert work.claim("owner", first)
        assert work.claim("other", second) is None
        assert work.last_denial is not None
        assert work.last_denial.prior.task_id == first
        assert work.last_denial.reason == "linked-contention"
        assert work.get(second) is None
        assert not db.conn.in_transaction
        assert work.claim("owner", second)
        assert work.last_denial is None


def test_admission_runs_for_dry_run_without_mutation(tmp_path: Path) -> None:
    with CoordinationDB(tmp_path / "coord.db") as db:
        work = LinkedWork(db)
        assert work.claim("owner", "task", dry_run=True)
        assert work.get("task") is None
        assert work.claim("owner", "task")
        changes = db.conn.total_changes
        assert work.claim("other", "issue", dry_run=True) is None
        assert db.conn.total_changes == changes
        assert not db.conn.in_transaction


def test_admission_error_rolls_back(tmp_path: Path) -> None:
    class BrokenWork(WorkClaimManager):
        def _claim_admission(
            self, agent_id: str, task_id: str
        ) -> WorkClaimDenial | None:
            raise RuntimeError("policy unavailable")

    with CoordinationDB(tmp_path / "coord.db") as db:
        work = BrokenWork(db)
        with pytest.raises(RuntimeError, match="policy unavailable"):
            work.claim("owner", "task")
        assert work.get("task") is None
        assert not db.conn.in_transaction


def _contend(db_path: str, key: str, barrier, results) -> None:
    with CoordinationDB(db_path) as db:
        work = LinkedWork(db)
        barrier.wait(timeout=15)
        results.put(work.claim(key + "-owner", key) is not None)


def test_linked_admission_serializes_two_processes(tmp_path: Path) -> None:
    path = str(tmp_path / "coord.db")
    with CoordinationDB(path) as db:
        LinkedWork(db)
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    results = ctx.Queue()
    processes = [
        ctx.Process(target=_contend, args=(path, key, barrier, results))
        for key in ("task", "issue")
    ]
    try:
        for process in processes:
            process.start()
        outcomes = [results.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
        assert sorted(outcomes) == [False, True]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()
        results.close()
