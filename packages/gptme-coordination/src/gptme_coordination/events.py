"""Event queue for gptme skill execution.

Provides a durable SQLite-backed event bus that decouples trigger detection
(PR updates, CI failures, scheduled tasks) from session dispatch. Supports
priority scoring, deduplication via thread_key, retry with dead-letter, and
backpressure gating.

State machine:
    pending → claimed → completed (via complete())
                    └→ pending/retry → dead_letter (via fail(), when retries exhausted)
                    └→ pending (via release_stale_claims(), crash recovery)
    dead_letter → pending (via retry(), manual)
    dead_letter → completed (via discard(), manual)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gptme_coordination.db import CoordinationDB

# Default age-boost cap: events gain at most this many priority points
# as they age (prevents very old events from monopolizing the queue).
AGE_BOOST_CAP = 20
AGE_BOOST_RATE = 2  # priority points per hour

# Default priority levels for common trigger types
PRIORITY_DEFAULTS: dict[str, int] = {
    "ci_failure_master": 100,
    "ci_failure_pr": 80,
    "assign": 70,
    "pr_update_approved": 60,
    "mention": 50,
    "pr_update_review": 40,
    "pr_update_general": 30,
    "scheduled_high": 20,
    "scheduled_low": 10,
}


@dataclass
class Event:
    """A single event in the queue."""

    id: int | None
    trigger_type: str
    source: str
    thread_key: str
    state: str  # pending, claimed, completed, dead_letter (processing is reserved)
    priority: int
    retry_count: int
    max_retries: int
    created_at: datetime
    # Optional fields
    external_id: str | None = None
    repo: str | None = None
    number: int | None = None
    title: str | None = None
    url: str | None = None
    payload_json: str | None = None
    claimed_at: datetime | None = None
    completed_at: datetime | None = None
    claimed_by: str | None = None
    batch_id: str | None = None
    result: str | None = None
    result_detail: str | None = None
    # Computed fields
    effective_priority: int = field(init=False)

    def __post_init__(self) -> None:
        age_hours = (
            datetime.now(UTC) - self.created_at.replace(tzinfo=UTC)
        ).total_seconds() / 3600
        boost = min(int(age_hours * AGE_BOOST_RATE), AGE_BOOST_CAP)
        self.effective_priority = self.priority + boost

    @property
    def payload(self) -> dict[str, Any]:
        if self.payload_json:
            return json.loads(self.payload_json)  # type: ignore[no-any-return]
        return {}


@dataclass
class QueueStats:
    """Summary statistics for the event queue."""

    pending: int
    claimed: int
    completed: int
    dead_letter: int
    oldest_pending_seconds: float | None


class EventQueue:
    """Manages the event queue via SQLite.

    Events are ingested by trigger sources (trigger-probe, health checks)
    and consumed by the queue processor, which dispatches sessions.
    """

    def __init__(self, db: CoordinationDB):
        self.db = db

    def ingest(
        self,
        trigger_type: str,
        source: str,
        thread_key: str,
        *,
        external_id: str | None = None,
        repo: str | None = None,
        number: int | None = None,
        title: str | None = None,
        url: str | None = None,
        payload: dict[str, Any] | None = None,
        priority: int | None = None,
        max_retries: int = 3,
        dedup_window_minutes: int = 30,
    ) -> Event | None:
        """Ingest an event into the queue.

        Deduplicates: if a pending/claimed event with the same thread_key
        exists within ``dedup_window_minutes``, returns None (skipped).

        Returns the created Event, or None if deduplicated.
        """
        resolved_priority = (
            priority if priority is not None else PRIORITY_DEFAULTS.get(trigger_type, 0)
        )
        payload_json = json.dumps(payload) if payload else None

        conn = self.db.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Dedup: check for recent pending/claimed event on the same thread
            existing = conn.execute(
                """SELECT id FROM events
                WHERE thread_key = ?
                  AND state IN ('pending', 'claimed', 'processing')
                  AND created_at > datetime('now', ? || ' minutes')""",
                (thread_key, str(-dedup_window_minutes)),
            ).fetchone()
            if existing:
                conn.execute("ROLLBACK")
                return None

            cursor = conn.execute(
                """INSERT INTO events
                    (trigger_type, source, external_id, thread_key,
                     repo, number, title, url, payload_json,
                     priority, max_retries)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trigger_type,
                    source,
                    external_id,
                    thread_key,
                    repo,
                    number,
                    title,
                    url,
                    payload_json,
                    resolved_priority,
                    max_retries,
                ),
            )
            event_id = cursor.lastrowid
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        return self.get(event_id)  # type: ignore[arg-type]

    def claim_next(self, claimed_by: str) -> Event | None:
        """Atomically claim the highest-priority pending event.

        Uses CAS to ensure only one agent claims a given event.
        Returns the claimed Event, or None if queue is empty.
        """
        conn = self.db.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Select the highest-priority pending event (age-boosted).
            # Age boost prevents low-priority events from starving: older events
            # gain a priority bonus of AGE_BOOST_RATE per hour, capped at AGE_BOOST_CAP.
            row = conn.execute(
                """SELECT id FROM events
                WHERE state = 'pending'
                ORDER BY (priority + MIN(CAST((julianday('now') - julianday(created_at)) * 24 * ? AS INTEGER), ?)) DESC, created_at ASC
                LIMIT 1""",
                (AGE_BOOST_RATE, AGE_BOOST_CAP),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None

            event_id = row["id"]
            conn.execute(
                """UPDATE events
                SET state = 'claimed', claimed_by = ?, claimed_at = datetime('now')
                WHERE id = ? AND state = 'pending'""",
                (claimed_by, event_id),
            )
            if conn.execute("SELECT changes()").fetchone()[0] == 0:
                conn.execute("ROLLBACK")
                return None

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        return self.get(event_id)

    def complete(
        self, event_id: int, *, result: str = "success", detail: str | None = None
    ) -> bool:
        """Mark an event as completed."""
        rows = self.db.conn.execute(
            """UPDATE events
            SET state = 'completed', result = ?, result_detail = ?,
                completed_at = datetime('now')
            WHERE id = ? AND state IN ('claimed', 'processing')""",
            (result, detail, event_id),
        )
        return bool(rows.rowcount > 0)

    def fail(self, event_id: int, *, detail: str | None = None) -> bool:
        """Mark an event as failed, scheduling retry or dead-letter.

        If retry_count < max_retries, resets to 'pending' with incremented retry_count.
        Otherwise moves to 'dead_letter'.
        """
        conn = self.db.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT retry_count, max_retries FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return False

            retry_count = row["retry_count"]
            max_retries = row["max_retries"]

            if retry_count < max_retries:
                # Schedule retry with exponential backoff (tracked via created_at reset)
                conn.execute(
                    """UPDATE events
                    SET state = 'pending', retry_count = retry_count + 1,
                        result = 'failure', result_detail = ?,
                        claimed_by = NULL, claimed_at = NULL
                    WHERE id = ? AND state IN ('claimed', 'processing')""",
                    (detail, event_id),
                )
            else:
                conn.execute(
                    """UPDATE events
                    SET state = 'dead_letter', retry_count = retry_count + 1,
                        result = 'failure', result_detail = ?,
                        completed_at = datetime('now')
                    WHERE id = ? AND state IN ('claimed', 'processing')""",
                    (detail, event_id),
                )

            ok = bool(conn.execute("SELECT changes()").fetchone()[0] > 0)
            conn.execute("COMMIT" if ok else "ROLLBACK")
            return ok
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def retry(self, event_id: int) -> bool:
        """Manually retry a dead-lettered event (resets retry count)."""
        rows = self.db.conn.execute(
            """UPDATE events
            SET state = 'pending', retry_count = 0, result = NULL,
                result_detail = NULL, claimed_by = NULL, claimed_at = NULL,
                completed_at = NULL
            WHERE id = ? AND state IN ('dead_letter')""",
            (event_id,),
        )
        return bool(rows.rowcount > 0)

    def discard(self, event_id: int) -> bool:
        """Discard an event (marks as completed with result='discarded')."""
        rows = self.db.conn.execute(
            """UPDATE events
            SET state = 'completed', result = 'discarded',
                completed_at = datetime('now')
            WHERE id = ? AND state NOT IN ('completed')""",
            (event_id,),
        )
        return bool(rows.rowcount > 0)

    def list_events(
        self,
        state: str | None = None,
        repo: str | None = None,
        limit: int = 50,
    ) -> list[Event]:
        """List events, optionally filtered by state or repo."""
        conditions = []
        params: list[Any] = []

        if state:
            conditions.append("state = ?")
            params.append(state)
        if repo:
            conditions.append("repo = ?")
            params.append(repo)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        rows = self.db.conn.execute(
            f"SELECT * FROM events {where} ORDER BY priority DESC, created_at ASC LIMIT ?",  # noqa: S608
            params,
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def get(self, event_id: int) -> Event | None:
        """Fetch a single event by ID."""
        row = self.db.conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        return _row_to_event(row) if row else None

    def stats(self) -> QueueStats:
        """Return queue depth and processing statistics."""
        rows = self.db.conn.execute(
            "SELECT state, COUNT(*) as cnt FROM events GROUP BY state"
        ).fetchall()
        counts = {r["state"]: r["cnt"] for r in rows}

        oldest = self.db.conn.execute(
            """SELECT MIN(created_at) as oldest FROM events WHERE state = 'pending'"""
        ).fetchone()
        oldest_pending_seconds: float | None = None
        if oldest and oldest["oldest"]:
            created = datetime.fromisoformat(oldest["oldest"])
            oldest_pending_seconds = (
                datetime.now(UTC) - created.replace(tzinfo=UTC)
            ).total_seconds()

        return QueueStats(
            pending=counts.get("pending", 0),
            claimed=counts.get("claimed", 0) + counts.get("processing", 0),
            completed=counts.get("completed", 0),
            dead_letter=counts.get("dead_letter", 0),
            oldest_pending_seconds=oldest_pending_seconds,
        )

    def release_stale_claims(self, older_than_minutes: int = 120) -> int:
        """Return stale claimed events to pending (for crashed processors).

        Events claimed more than ``older_than_minutes`` ago without completion
        are assumed abandoned and reset to pending for retry.

        Increments ``retry_count`` so that a consistently crashing processor
        will eventually hit ``max_retries`` on the next ``fail()`` call and
        dead-letter the event.
        """
        rows = self.db.conn.execute(
            """UPDATE events
            SET state = 'pending', claimed_by = NULL, claimed_at = NULL,
                retry_count = retry_count + 1
            WHERE state IN ('claimed', 'processing')
              AND claimed_at < datetime('now', ? || ' minutes')""",
            (str(-older_than_minutes),),
        )
        return rows.rowcount


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _row_to_event(row: Any) -> Event:
    """Convert a sqlite3.Row to an Event dataclass."""
    return Event(
        id=row["id"],
        trigger_type=row["trigger_type"],
        source=row["source"],
        thread_key=row["thread_key"],
        state=row["state"],
        priority=row["priority"],
        retry_count=row["retry_count"],
        max_retries=row["max_retries"],
        created_at=_parse_dt(row["created_at"]) or datetime.now(UTC),
        external_id=row["external_id"],
        repo=row["repo"],
        number=row["number"],
        title=row["title"],
        url=row["url"],
        payload_json=row["payload_json"],
        claimed_at=_parse_dt(row["claimed_at"]),
        completed_at=_parse_dt(row["completed_at"]),
        claimed_by=row["claimed_by"],
        batch_id=row["batch_id"],
        result=row["result"],
        result_detail=row["result_detail"],
    )
