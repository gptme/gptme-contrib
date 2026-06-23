"""Tests for the event queue (Phase 1: schema + ingest)."""

from __future__ import annotations

import pytest
from gptme_coordination.db import CoordinationDB
from gptme_coordination.events import PRIORITY_DEFAULTS, EventQueue, QueueStats


@pytest.fixture
def db(tmp_path):
    db_file = tmp_path / "test_events.db"
    with CoordinationDB(db_file) as db:
        yield db


@pytest.fixture
def queue(db):
    return EventQueue(db)


class TestIngest:
    def test_ingest_creates_pending_event(self, queue):
        ev = queue.ingest("pr_update_general", "github", "gptme/gptme:100")
        assert ev is not None
        assert ev.id is not None
        assert ev.state == "pending"
        assert ev.trigger_type == "pr_update_general"
        assert ev.source == "github"
        assert ev.thread_key == "gptme/gptme:100"
        assert ev.retry_count == 0
        assert ev.max_retries == 3

    def test_ingest_default_priority_from_trigger_type(self, queue):
        ev = queue.ingest("ci_failure_master", "github", "gptme/gptme:master")
        assert ev is not None
        assert ev.priority == PRIORITY_DEFAULTS["ci_failure_master"]

    def test_ingest_override_priority(self, queue):
        ev = queue.ingest("pr_update_general", "github", "gptme/gptme:100", priority=99)
        assert ev is not None
        assert ev.priority == 99

    def test_ingest_unknown_trigger_type_defaults_zero(self, queue):
        ev = queue.ingest("exotic_custom_type", "custom", "custom:key")
        assert ev is not None
        assert ev.priority == 0

    def test_ingest_with_metadata(self, queue):
        ev = queue.ingest(
            "pr_update_review",
            "github",
            "gptme/gptme-contrib:50",
            repo="gptme/gptme-contrib",
            number=50,
            title="fix: some bug",
            url="https://github.com/gptme/gptme-contrib/pull/50",
            payload={"action": "submitted", "state": "changes_requested"},
        )
        assert ev is not None
        assert ev.repo == "gptme/gptme-contrib"
        assert ev.number == 50
        assert ev.title == "fix: some bug"
        assert ev.url == "https://github.com/gptme/gptme-contrib/pull/50"
        assert ev.payload["action"] == "submitted"

    def test_ingest_deduplicates_within_window(self, queue):
        ev1 = queue.ingest("pr_update_general", "github", "gptme/gptme:100")
        ev2 = queue.ingest("pr_update_general", "github", "gptme/gptme:100")
        assert ev1 is not None
        assert ev2 is None  # deduplicated

    def test_ingest_different_thread_keys_not_deduplicated(self, queue):
        ev1 = queue.ingest("pr_update_general", "github", "gptme/gptme:100")
        ev2 = queue.ingest("pr_update_general", "github", "gptme/gptme:101")
        assert ev1 is not None
        assert ev2 is not None
        assert ev1.id != ev2.id

    def test_ingest_allows_retry_after_completion(self, queue):
        ev = queue.ingest("pr_update_general", "github", "gptme/gptme:200")
        assert ev is not None
        # Must claim before completing
        claimed = queue.claim_next("agent-1")
        assert claimed is not None
        queue.complete(ev.id, result="success")
        # After completion, new event on same thread should be allowed
        ev2 = queue.ingest("pr_update_general", "github", "gptme/gptme:200")
        assert ev2 is not None


class TestClaimAndComplete:
    def test_claim_next_returns_highest_priority(self, queue):
        queue.ingest("pr_update_general", "github", "key1", priority=10)
        queue.ingest("ci_failure_master", "github", "key2", priority=100)
        queue.ingest("mention", "github", "key3", priority=50)

        ev = queue.claim_next("agent-1")
        assert ev is not None
        assert ev.priority == 100
        assert ev.state == "claimed"
        assert ev.claimed_by == "agent-1"

    def test_claim_next_empty_queue_returns_none(self, queue):
        assert queue.claim_next("agent-1") is None

    def test_complete_marks_event_done(self, queue):
        ev = queue.ingest("pr_update_general", "github", "key1")
        assert ev is not None
        queue.claim_next("agent-1")
        ok = queue.complete(ev.id, result="success", detail="session completed")
        assert ok
        fetched = queue.get(ev.id)
        assert fetched is not None
        assert fetched.state == "completed"
        assert fetched.result == "success"
        assert fetched.result_detail == "session completed"

    def test_complete_unclaimed_event_fails(self, queue):
        ev = queue.ingest("pr_update_general", "github", "key1")
        assert ev is not None
        ok = queue.complete(ev.id, result="success")
        assert not ok


class TestRetryAndDeadLetter:
    def test_fail_schedules_retry_when_retries_remain(self, queue):
        ev = queue.ingest("pr_update_general", "github", "key1", max_retries=3)
        assert ev is not None
        queue.claim_next("agent-1")
        ok = queue.fail(ev.id, detail="timeout")
        assert ok
        fetched = queue.get(ev.id)
        assert fetched is not None
        assert fetched.state == "pending"
        assert fetched.retry_count == 1

    def test_fail_moves_to_dead_letter_when_retries_exhausted(self, queue):
        ev = queue.ingest("pr_update_general", "github", "key1", max_retries=1)
        assert ev is not None
        # Fail once → retry
        queue.claim_next("agent-1")
        queue.fail(ev.id)
        # Fail again → dead_letter
        queue.claim_next("agent-1")
        ok = queue.fail(ev.id)
        assert ok
        fetched = queue.get(ev.id)
        assert fetched is not None
        assert fetched.state == "dead_letter"

    def test_retry_resets_dead_lettered_event(self, queue):
        ev = queue.ingest("pr_update_general", "github", "key1", max_retries=0)
        assert ev is not None
        queue.claim_next("agent-1")
        queue.fail(ev.id)
        # Should be dead_letter now
        fetched = queue.get(ev.id)
        assert fetched is not None
        assert fetched.state == "dead_letter"
        # Manual retry
        ok = queue.retry(ev.id)
        assert ok
        fetched = queue.get(ev.id)
        assert fetched is not None
        assert fetched.state == "pending"
        assert fetched.retry_count == 0

    def test_discard_removes_event_from_active_queue(self, queue):
        ev = queue.ingest("pr_update_general", "github", "key1")
        assert ev is not None
        ok = queue.discard(ev.id)
        assert ok
        fetched = queue.get(ev.id)
        assert fetched is not None
        assert fetched.state == "completed"
        assert fetched.result == "discarded"


class TestListAndStats:
    def test_list_all_events(self, queue):
        queue.ingest("pr_update_general", "github", "key1")
        queue.ingest("ci_failure_pr", "github", "key2")
        events = queue.list_events()
        assert len(events) == 2

    def test_list_filter_by_state(self, queue):
        ev1 = queue.ingest("pr_update_general", "github", "key1")
        ev2 = queue.ingest("ci_failure_pr", "github", "key2")
        assert ev1 is not None
        assert ev2 is not None
        queue.claim_next("agent-1")
        pending = queue.list_events(state="pending")
        claimed = queue.list_events(state="claimed")
        assert len(pending) == 1
        assert len(claimed) == 1

    def test_list_filter_by_repo(self, queue):
        queue.ingest(
            "pr_update_general", "github", "gptme/gptme:100", repo="gptme/gptme"
        )
        queue.ingest(
            "pr_update_general",
            "github",
            "gptme/gptme-contrib:50",
            repo="gptme/gptme-contrib",
        )
        events = queue.list_events(repo="gptme/gptme")
        assert len(events) == 1
        assert events[0].repo == "gptme/gptme"

    def test_stats_empty_queue(self, queue):
        stats = queue.stats()
        assert isinstance(stats, QueueStats)
        assert stats.pending == 0
        assert stats.claimed == 0
        assert stats.completed == 0
        assert stats.dead_letter == 0
        assert stats.oldest_pending_seconds is None

    def test_stats_with_events(self, queue):
        queue.ingest("pr_update_general", "github", "key1")
        queue.ingest("ci_failure_pr", "github", "key2")
        ev = queue.claim_next("agent-1")
        assert ev is not None
        queue.complete(ev.id)

        stats = queue.stats()
        assert stats.pending == 1
        assert stats.claimed == 0
        assert stats.completed == 1


class TestStaleClaims:
    def test_release_stale_claims(self, queue):
        ev = queue.ingest("pr_update_general", "github", "key1")
        assert ev is not None
        queue.claim_next("agent-1")
        # Force the claimed_at to be in the past
        queue.db.conn.execute(
            "UPDATE events SET claimed_at = datetime('now', '-3 hours') WHERE id = ?",
            (ev.id,),
        )
        released = queue.release_stale_claims(older_than_minutes=120)
        assert released == 1
        fetched = queue.get(ev.id)
        assert fetched is not None
        assert fetched.state == "pending"
        assert fetched.retry_count == 1  # retry_count is bumped on stale release

    def test_stale_release_retry_count_enables_dead_letter(self, queue):
        """Crash-looping processor: stale releases bump retry_count so
        max_retries is eventually reached when the processor fails."""
        ev = queue.ingest("pr_update_general", "github", "key1", max_retries=1)
        assert ev is not None
        # Simulate claim → crash → stale release loop
        for _ in range(2):
            queue.claim_next("agent-1")
            queue.db.conn.execute(
                "UPDATE events SET claimed_at = datetime('now', '-3 hours') WHERE id = ?",
                (ev.id,),
            )
            queue.release_stale_claims(older_than_minutes=120)
        # After 2 stale releases, retry_count should be 2 (past max_retries=1)
        fetched = queue.get(ev.id)
        assert fetched is not None
        assert fetched.retry_count == 2
        # Next claim + explicit fail should dead-letter (retry_count >= max_retries)
        queue.claim_next("agent-1")
        queue.fail(ev.id)
        fetched = queue.get(ev.id)
        assert fetched is not None
        assert fetched.state == "dead_letter"
