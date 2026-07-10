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

    def test_complete_after_expiry_is_denied(self, work: WorkClaimManager) -> None:
        """An expired claim must not be completable (ghost-complete prevention)."""
        work.claim("agent-a", "task-1")
        work.db.conn.execute(
            "UPDATE work SET expires_at = datetime('now', '-1 seconds') WHERE task_id = ?",
            ("task-1",),
        )
        ok = work.complete("agent-a", "task-1")
        assert not ok
        # Task should remain in claimed state, reclaimable by another agent
        claim = work.get("task-1")
        assert claim is not None
        assert claim.status == "claimed"


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


class TestVacuumExpired:
    def test_vacuum_completed_uses_completed_at(self, work: WorkClaimManager) -> None:
        """vacuum_expired for completed tasks must age by completed_at, not claimed_at."""
        work.claim("agent-a", "task-old")
        work.complete("agent-a", "task-old")

        # Backdating claimed_at to > 24h ago while completed_at is recent
        work.db.conn.execute(
            "UPDATE work SET claimed_at = datetime('now', '-48 hours') WHERE task_id = ?",
            ("task-old",),
        )
        work.db.conn.commit()

        # Should NOT delete — completed_at is recent (< 24h)
        counts = work.vacuum_expired(completed_age_hours=24)
        assert counts.get("completed_older_than_24h", 0) == 0
        assert work.get("task-old") is not None

    def test_vacuum_completed_respects_completed_at_age(
        self, work: WorkClaimManager
    ) -> None:
        """Tasks completed long ago ARE swept when completed_at is old enough."""
        work.claim("agent-a", "task-old")
        work.complete("agent-a", "task-old")

        # Backdate completed_at
        work.db.conn.execute(
            "UPDATE work SET completed_at = datetime('now', '-48 hours') WHERE task_id = ?",
            ("task-old",),
        )
        work.db.conn.commit()

        counts = work.vacuum_expired(completed_age_hours=24)
        assert counts.get("completed_older_than_24h", 0) == 1
        assert work.get("task-old") is None


class TestHMACStaleOnReclaim:
    def test_reclaim_without_secret_clears_hmac(self, work: WorkClaimManager) -> None:
        """Reclaiming a task without a secret must clear any stale HMAC."""
        secret = b"mysecret"
        work.claim("agent-a", "task-1", secret=secret)
        work.complete("agent-a", "task-1")

        # Reclaim without a secret — stale HMAC must be cleared
        reclaimed = work.claim("agent-b", "task-1")
        assert reclaimed is not None
        assert (
            reclaimed.hmac is None
        ), "stale HMAC should be cleared on secretless reclaim"
        assert reclaimed.verified is False

    def test_reclaim_with_secret_refreshes_hmac(self, work: WorkClaimManager) -> None:
        """Reclaiming with a secret computes a fresh HMAC over the new claim fields."""
        secret = b"mysecret"
        work.claim("agent-a", "task-1", secret=secret)
        work.complete("agent-a", "task-1")

        new_secret = b"newsecret"
        reclaimed = work.claim("agent-b", "task-1", secret=new_secret)
        assert reclaimed is not None
        assert reclaimed.hmac is not None

    def test_extend_without_secret_clears_hmac(self, work: WorkClaimManager) -> None:
        """Extending our own claim without a secret must not keep the stale HMAC."""
        secret = b"mysecret"
        work.claim("agent-a", "task-1", secret=secret)

        # Extend without a secret
        extended = work.claim("agent-a", "task-1")
        assert extended is not None
        assert (
            extended.hmac is None
        ), "stale HMAC should be cleared on secretless extend"

    def test_verified_false_on_read(self, work: WorkClaimManager) -> None:
        """Claims read from DB are never marked verified=True; only verify_claim() does that."""
        secret = b"mysecret"
        work.claim("agent-a", "task-1", secret=secret)
        claim = work.get("task-1")
        assert claim is not None
        assert claim.verified is False

    def test_abandon_clears_hmac(self, work: WorkClaimManager) -> None:
        """abandon() must clear the HMAC so the row doesn't carry a stale signature."""
        secret = b"mysecret"
        work.claim("agent-a", "task-1", secret=secret)

        # Verify the HMAC was stored
        claimed = work.get("task-1")
        assert claimed is not None
        assert claimed.hmac is not None

        # Abandon the task
        assert work.abandon("agent-a", "task-1") is True

        # HMAC must be cleared on the abandoned row
        abandoned = work.get("task-1")
        assert abandoned is not None
        assert abandoned.status == "abandoned"
        assert abandoned.hmac is None, "abandon() must clear stale HMAC"


class TestExpiryHmacCoherence:
    """The stored HMAC must always verify against the stored expires_at.

    Regression test for a bug where the HMAC was computed over a
    Python-clock expiry string, while a *separate* SQLite-evaluated
    ``datetime('now', ...)`` call computed the stored ``expires_at``. When
    the two clock reads straddled a second boundary, the stored HMAC could
    never verify against the stored row. This held only probabilistically
    before the fix (two clock reads) and holds by construction after it
    (single clock read reused for both).
    """

    def test_fresh_claim_hmac_matches_stored_expiry(
        self, work: WorkClaimManager
    ) -> None:
        from gptme_coordination.auth import verify_hmac

        secret = b"s"
        claim = work.claim("agent-x", "task-fresh", ttl_minutes=60, secret=secret)
        assert claim is not None

        row = work.db.conn.execute(
            "SELECT expires_at, epoch, hmac FROM work WHERE task_id = ?",
            ("task-fresh",),
        ).fetchone()
        assert verify_hmac(
            secret,
            row["hmac"],
            "agent-x",
            "task-fresh",
            row["epoch"],
            row["expires_at"],
        )

    def test_reclaim_of_expired_claim_hmac_matches_stored_expiry(
        self, work: WorkClaimManager
    ) -> None:
        from gptme_coordination.auth import verify_hmac

        secret = b"s"
        work.claim("agent-a", "task-expired", ttl_minutes=60, secret=secret)
        # Manually backdate expires_at to force the CAS "expired claim" path.
        work.db.conn.execute(
            "UPDATE work SET expires_at = datetime('now', '-1 seconds') WHERE task_id = ?",
            ("task-expired",),
        )

        claim = work.claim("agent-b", "task-expired", ttl_minutes=60, secret=secret)
        assert claim is not None
        assert claim.claimer == "agent-b"

        row = work.db.conn.execute(
            "SELECT expires_at, epoch, hmac FROM work WHERE task_id = ?",
            ("task-expired",),
        ).fetchone()
        assert verify_hmac(
            secret,
            row["hmac"],
            "agent-b",
            "task-expired",
            row["epoch"],
            row["expires_at"],
        )

    def test_reclaim_of_completed_task_hmac_matches_stored_expiry(
        self, work: WorkClaimManager
    ) -> None:
        from gptme_coordination.auth import verify_hmac

        secret = b"s"
        work.claim("agent-a", "task-completed", ttl_minutes=60, secret=secret)
        work.complete("agent-a", "task-completed")

        claim = work.claim("agent-b", "task-completed", ttl_minutes=60, secret=secret)
        assert claim is not None
        assert claim.claimer == "agent-b"

        row = work.db.conn.execute(
            "SELECT expires_at, epoch, hmac FROM work WHERE task_id = ?",
            ("task-completed",),
        ).fetchone()
        assert verify_hmac(
            secret,
            row["hmac"],
            "agent-b",
            "task-completed",
            row["epoch"],
            row["expires_at"],
        )

    def test_extend_own_claim_hmac_matches_stored_expiry(
        self, work: WorkClaimManager
    ) -> None:
        from gptme_coordination.auth import verify_hmac

        secret = b"s"
        work.claim("agent-a", "task-extend", ttl_minutes=60, secret=secret)

        claim = work.claim("agent-a", "task-extend", ttl_minutes=60, secret=secret)
        assert claim is not None
        assert claim.claimer == "agent-a"

        row = work.db.conn.execute(
            "SELECT expires_at, epoch, hmac FROM work WHERE task_id = ?",
            ("task-extend",),
        ).fetchone()
        assert verify_hmac(
            secret,
            row["hmac"],
            "agent-a",
            "task-extend",
            row["epoch"],
            row["expires_at"],
        )


class TestAuthCompatibility:
    def test_verify_hmac_matches_work_claim_manager(
        self, work: WorkClaimManager
    ) -> None:
        """auth.verify_hmac must validate signatures produced by WorkClaimManager."""
        from gptme_coordination.auth import verify_hmac

        secret = b"testsecret"
        work.claim("agent-a", "task-1", secret=secret)
        claim = work.get("task-1")
        assert claim is not None
        assert claim.hmac is not None

        # auth.verify_hmac uses the same JSON encoding as WorkClaimManager.compute_hmac;
        # epoch is int and expires_at is str (as stored in the managers)
        expires_at_str = (
            claim.expires_at.strftime("%Y-%m-%d %H:%M:%S") if claim.expires_at else None
        )
        assert verify_hmac(
            secret, claim.hmac, "agent-a", "task-1", claim.epoch, expires_at_str
        ), "auth.verify_hmac encoding must match WorkClaimManager.compute_hmac"
