"""Tests for CAS-based work (task) claiming."""

from pathlib import Path

import pytest
from gptme_coordination.db import CoordinationDB
from gptme_coordination.work import WorkClaimManager


@pytest.fixture
def db(tmp_path: Path) -> CoordinationDB:
    return CoordinationDB(tmp_path / "test.db")


@pytest.fixture
def work(db: CoordinationDB) -> WorkClaimManager:
    return WorkClaimManager(db, default_ttl_minutes=60)


class TestSubmit:
    def test_submit_new_task(self, work: WorkClaimManager) -> None:
        claim = work.submit("task-1")
        assert claim.task_id == "task-1"
        assert claim.status == "available"
        assert claim.claimer is None

    def test_submit_with_metadata(self, work: WorkClaimManager) -> None:
        claim = work.submit("task-1", metadata="fix bug in parser")
        assert claim.metadata == "fix bug in parser"

    def test_submit_idempotent(self, work: WorkClaimManager) -> None:
        work.submit("task-1", metadata="first")
        claim = work.submit("task-1", metadata="second")
        assert claim.status == "available"


class TestClaim:
    def test_claim_available_task(self, work: WorkClaimManager) -> None:
        work.submit("task-1")
        claim = work.claim("agent-a", "task-1")
        assert claim is not None
        assert claim.claimer == "agent-a"
        assert claim.status == "claimed"

    def test_claim_nonexistent_auto_submits(self, work: WorkClaimManager) -> None:
        claim = work.claim("agent-a", "task-new")
        assert claim is not None
        assert claim.status == "claimed"

    def test_claim_held_by_other_denied(self, work: WorkClaimManager) -> None:
        work.claim("agent-a", "task-1")
        claim = work.claim("agent-b", "task-1")
        assert claim is None

    def test_claim_own_extends_ttl(self, work: WorkClaimManager) -> None:
        c1 = work.claim("agent-a", "task-1")
        assert c1 is not None
        c2 = work.claim("agent-a", "task-1")
        assert c2 is not None
        assert c2.claimer == "agent-a"

    def test_completed_task_reclaim_default_allowed(
        self, work: WorkClaimManager
    ) -> None:
        work.claim("agent-a", "task-1")
        work.complete("agent-a", "task-1")
        # No on_completed_check → allow reclaiming
        claim = work.claim("agent-b", "task-1")
        assert claim is not None
        assert claim.claimer == "agent-b"

    def test_completed_task_reclaim_blocked_by_callback(self, tmp_path: Path) -> None:
        db = CoordinationDB(tmp_path / "cb.db")
        work_cb = WorkClaimManager(db, on_completed_check=lambda tid, dbp: False)
        work_cb.claim("agent-a", "task-x")
        work_cb.complete("agent-a", "task-x")
        claim = work_cb.claim("agent-b", "task-x")
        assert claim is None

    def test_completed_task_reclaim_allowed_by_callback(self, tmp_path: Path) -> None:
        db = CoordinationDB(tmp_path / "cb.db")
        work_cb = WorkClaimManager(db, on_completed_check=lambda tid, dbp: True)
        work_cb.claim("agent-a", "task-x")
        work_cb.complete("agent-a", "task-x")
        claim = work_cb.claim("agent-b", "task-x")
        assert claim is not None


class TestComplete:
    def test_complete_claimed_task(self, work: WorkClaimManager) -> None:
        work.claim("agent-a", "task-1")
        ok = work.complete("agent-a", "task-1", result="done")
        assert ok
        claim = work.get("task-1")
        assert claim is not None
        assert claim.status == "completed"
        assert claim.result == "done"

    def test_complete_not_held(self, work: WorkClaimManager) -> None:
        work.submit("task-1")
        ok = work.complete("agent-a", "task-1")
        assert not ok


class TestAbandon:
    def test_abandon_claimed_task(self, work: WorkClaimManager) -> None:
        work.claim("agent-a", "task-1")
        ok = work.abandon("agent-a", "task-1")
        assert ok
        claim = work.get("task-1")
        assert claim is not None
        assert claim.status == "abandoned"


class TestList:
    def test_list_all(self, work: WorkClaimManager) -> None:
        work.submit("t1")
        work.submit("t2")
        work.claim("agent-a", "t2")
        all_claims = work.list_all()
        assert len(all_claims) == 2

    def test_list_available(self, work: WorkClaimManager) -> None:
        work.submit("t1")
        work.submit("t2")
        work.claim("agent-a", "t2")
        available = work.list_available()
        assert len(available) == 1
        assert available[0].task_id == "t1"

    def test_list_claimed(self, work: WorkClaimManager) -> None:
        work.claim("agent-a", "t1")
        work.claim("agent-b", "t2")
        claimed = work.list_claimed()
        assert len(claimed) == 2
        claimed_a = work.list_claimed(agent_id="agent-a")
        assert len(claimed_a) == 1
