"""CAS-based work (task) claiming for multi-agent coordination.

Agents atomically claim tasks so that only one agent works on a given
task at a time. Uses the same CAS pattern as file leases:

    UPDATE work SET claimer=?, epoch=epoch+1
    WHERE task_id=? AND (claimer IS NULL OR expires_at < datetime('now'))

Task claims auto-expire after a configurable TTL (default 60 minutes),
so crashed agents don't permanently lock tasks.

Every claim operation includes an HMAC over the (claimer|task_id|epoch|expires_at)
tuple to authenticate the claiming agent's identity, preventing forged claims
by agents asserting another agent's identity.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from gptme_coordination.auth import compute_hmac as _compute_hmac
from gptme_coordination.db import CoordinationDB

DEFAULT_WORK_TTL_MINUTES = 60

WORK_SCHEMA = """
CREATE TABLE IF NOT EXISTS work (
    task_id TEXT PRIMARY KEY,
    claimer TEXT,
    epoch INTEGER NOT NULL DEFAULT 0,
    claimed_at TEXT,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'available',
    result TEXT,
    metadata TEXT,
    completed_at TEXT,
    hmac TEXT
);

CREATE INDEX IF NOT EXISTS idx_work_claimer ON work(claimer);
CREATE INDEX IF NOT EXISTS idx_work_status ON work(status);
"""


@dataclass
class WorkClaim:
    task_id: str
    claimer: str | None
    epoch: int
    claimed_at: datetime | None
    expires_at: datetime | None
    status: str  # available, claimed, completed, abandoned
    result: str | None = None
    metadata: str | None = None
    completed_at: datetime | None = None
    hmac: str | None = None
    verified: bool = False  # True = HMAC validates against known secret


@dataclass(frozen=True)
class WorkReopenWarning:
    """Advisory warning for reopening task work owned in the coordination DB."""

    task_id: str
    status: str
    claimer: str | None
    event_at: datetime | None
    expires_at: datetime | None
    result: str | None = None


REOPEN_SOURCE_STATES = {"done"}
REOPEN_TARGET_STATES = {"active", "backlog"}


def _normalize_datetime(value: datetime | None) -> datetime | None:
    """Normalize datetimes to naive UTC for SQLite timestamp comparisons."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def should_warn_on_task_reopen(
    claim: WorkClaim,
    *,
    previous_state: str,
    new_state: str,
    actor_id: str | None = None,
    now: datetime | None = None,
    recent_hours: int = 6,
) -> bool:
    """Return True when a state transition should warn about existing work.

    This is intentionally advisory. Reopening a terminal task can be valid, but
    a recent foreign completion or active foreign claim is strong evidence that
    the operator should check the already-landed work before reimplementing it.
    """
    if (
        previous_state not in REOPEN_SOURCE_STATES
        or new_state not in REOPEN_TARGET_STATES
    ):
        return False
    if actor_id and claim.claimer == actor_id:
        return False

    current_time = _normalize_datetime(now or datetime.now(UTC))
    assert current_time is not None

    if claim.status == "claimed":
        expires_at = _normalize_datetime(claim.expires_at)
        return expires_at is None or expires_at >= current_time

    if claim.status != "completed":
        return False

    event_at = _normalize_datetime(claim.completed_at or claim.claimed_at)
    if event_at is None:
        return False
    return current_time - event_at <= timedelta(hours=recent_hours)


class WorkClaimManager:
    """Manages task claims via SQLite CAS operations.

    Tasks go through these states:
        available → claimed → completed
                          └→ abandoned (on expiry or explicit abandon)

    ``on_completed_check`` is an optional callback invoked when an agent
    attempts to reclaim a *completed* task.  It receives ``(task_id,
    db_path)`` and returns ``True`` to allow the reclaim or ``False`` to deny
    it.  When omitted, completed tasks can always be reclaimed — useful for
    generic agents that don't have workspace-level task-state awareness.
    Agent-specific packages (e.g. Bob's ``coordination`` package) inject a
    callback that checks whether the workspace task has been explicitly
    reopened before allowing the reclaim.
    """

    def __init__(
        self,
        db: CoordinationDB,
        default_ttl_minutes: int = DEFAULT_WORK_TTL_MINUTES,
        on_completed_check: Callable[[str, str | None], bool] | None = None,
    ):
        self.db = db
        self.default_ttl_minutes = default_ttl_minutes
        self.on_completed_check = on_completed_check
        # Ensure work table exists
        self.db.conn.executescript(WORK_SCHEMA)
        self._migrate_work_schema()

    def _migrate_work_schema(self) -> None:
        """Apply work-table migrations owned by the work manager."""
        try:
            self.db.conn.execute("ALTER TABLE work ADD COLUMN completed_at TEXT")
        except Exception:
            pass  # Column already exists.

    @staticmethod
    def compute_hmac(
        claimer: str,
        task_id: str,
        epoch: int,
        expires_at: str | None,
        secret: bytes,
    ) -> str:
        """HMAC-SHA256 over canonical (claimer, task_id, epoch, expires_at) JSON."""
        return _compute_hmac(secret, claimer, task_id, epoch, expires_at)

    def submit(
        self,
        task_id: str,
        metadata: str | None = None,
    ) -> WorkClaim:
        """Submit a task as available for claiming.

        If the task already exists and is available, this is a no-op.
        If it was abandoned, it resets to available.
        """
        conn = self.db.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status FROM work WHERE task_id = ?", (task_id,)
            ).fetchone()

            if row is None:
                conn.execute(
                    """INSERT INTO work (task_id, status, metadata)
                    VALUES (?, 'available', ?)""",
                    (task_id, metadata),
                )
            elif row["status"] == "abandoned":
                conn.execute(
                    """UPDATE work SET status = 'available', claimer = NULL,
                        claimed_at = NULL, expires_at = NULL, completed_at = NULL,
                        result = NULL, metadata = COALESCE(?, metadata)
                    WHERE task_id = ?""",
                    (metadata, task_id),
                )
            # If available or claimed or completed, leave as-is

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        return self.get(task_id)  # type: ignore[return-value]

    def claim(
        self,
        agent_id: str,
        task_id: str,
        ttl_minutes: int | None = None,
        metadata: str | None = None,
        secret: bytes | None = None,
        dry_run: bool = False,
    ) -> WorkClaim | None:
        """Attempt to claim a task.

        If ``secret`` is provided, stores an HMAC authenticating the claimer
        identity. Without a secret, ``hmac`` is NULL (legacy mode).

        Returns the WorkClaim if successful, None if another agent holds it
        or the task doesn't exist / is already completed without a reopen check.
        When ``dry_run`` is true, validates the claim path and returns the
        predicted claim without persisting it.
        Uses CAS to ensure exactly one winner under concurrent access.
        """
        ttl = ttl_minutes or self.default_ttl_minutes
        conn = self.db.conn

        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT claimer, expires_at, status, epoch, hmac FROM work WHERE task_id = ?",
                (task_id,),
            ).fetchone()

            if row is None:
                # Task doesn't exist — auto-submit and claim it
                hmac_val = None
                if secret is not None:
                    py_expires = (datetime.now(UTC) + timedelta(minutes=ttl)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    hmac_val = self.compute_hmac(
                        agent_id,
                        task_id,
                        1,
                        py_expires,
                        secret,
                    )
                conn.execute(
                    """INSERT INTO work (task_id, claimer, epoch, claimed_at,
                        expires_at, status, metadata, hmac)
                    VALUES (?, ?, 1, datetime('now'),
                        datetime('now', ? || ' minutes'), 'claimed', ?, ?)""",
                    (task_id, agent_id, str(ttl), metadata, hmac_val),
                )
            elif row["status"] == "completed":
                # Allow reclaiming if no check or check passes
                check = self.on_completed_check
                if check is not None and not check(task_id, str(self.db.db_path)):
                    conn.execute("ROLLBACK")
                    return None

                new_epoch = int(row["epoch"]) + 1
                hmac_val = None
                if secret is not None:
                    py_expires = (datetime.now(UTC) + timedelta(minutes=ttl)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    hmac_val = self.compute_hmac(
                        agent_id,
                        task_id,
                        new_epoch,
                        py_expires,
                        secret,
                    )

                conn.execute(
                    """UPDATE work
                    SET claimer = ?, epoch = epoch + 1,
                        claimed_at = datetime('now'),
                        expires_at = datetime('now', ? || ' minutes'),
                        status = 'claimed',
                        result = NULL,
                        completed_at = NULL,
                        metadata = COALESCE(?, metadata),
                        hmac = ?
                    WHERE task_id = ? AND status = 'completed'""",
                    (agent_id, str(ttl), metadata, hmac_val, task_id),
                )
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    conn.execute("ROLLBACK")
                    return None
            elif row["status"] in ("available", "abandoned") or (
                row["status"] == "claimed"
                and row["expires_at"] is not None
                and row["expires_at"] < datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
            ):
                # Available, abandoned, or expired claim — CAS update
                new_epoch = (
                    conn.execute(
                        "SELECT epoch FROM work WHERE task_id = ?", (task_id,)
                    ).fetchone()["epoch"]
                    + 1
                )
                hmac_val = None

                if secret is not None:
                    py_expires = (datetime.now(UTC) + timedelta(minutes=ttl)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    hmac_val = self.compute_hmac(
                        agent_id,
                        task_id,
                        new_epoch,
                        py_expires,
                        secret,
                    )

                conn.execute(
                    """UPDATE work
                    SET claimer = ?, epoch = epoch + 1,
                        claimed_at = datetime('now'),
                        expires_at = datetime('now', ? || ' minutes'),
                        status = 'claimed',
                        result = NULL,
                        completed_at = NULL,
                        metadata = COALESCE(?, metadata),
                        hmac = ?
                    WHERE task_id = ?
                        AND (status IN ('available', 'abandoned')
                             OR (status = 'claimed'
                                 AND expires_at < datetime('now')))""",
                    (agent_id, str(ttl), metadata, hmac_val, task_id),
                )
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    conn.execute("ROLLBACK")
                    return None
            elif row["status"] == "claimed" and row["claimer"] == agent_id:
                # We already hold it — extend the claim
                # Recompute HMAC when we have a secret (expiry changes, so old HMAC is stale)
                hmac_val = None
                if secret is not None:
                    py_expires = (datetime.now(UTC) + timedelta(minutes=ttl)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    new_epoch = (
                        row["epoch"]
                        if "epoch" in row.keys()
                        else (
                            conn.execute(
                                "SELECT epoch FROM work WHERE task_id = ?", (task_id,)
                            ).fetchone()["epoch"]
                        )
                    )
                    hmac_val = self.compute_hmac(
                        agent_id,
                        task_id,
                        new_epoch,
                        py_expires,
                        secret,
                    )
                conn.execute(
                    """UPDATE work
                    SET expires_at = datetime('now', ? || ' minutes'),
                        metadata = COALESCE(?, metadata),
                        hmac = ?
                    WHERE task_id = ? AND claimer = ?""",
                    (str(ttl), metadata, hmac_val, task_id, agent_id),
                )
            else:
                # Held by another agent and not expired
                conn.execute("ROLLBACK")
                return None

            preview = self.get(task_id)
            conn.execute("ROLLBACK" if dry_run else "COMMIT")
            return preview

        except Exception:
            conn.execute("ROLLBACK")
            raise

    def complete(
        self,
        agent_id: str,
        task_id: str,
        result: str | None = None,
        dry_run: bool = False,
    ) -> bool:
        """Mark a claimed task as completed. Returns True if successful.

        When ``dry_run`` is true, validates the completion path without
        persisting the status change.
        """
        conn = self.db.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                """UPDATE work SET status = 'completed', result = ?,
                    completed_at = datetime('now')
                WHERE task_id = ? AND claimer = ? AND status = 'claimed'""",
                (result, task_id, agent_id),
            )
            ok = bool(rows.rowcount > 0)
            conn.execute("ROLLBACK" if dry_run else "COMMIT")
            return ok
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def abandon(self, agent_id: str, task_id: str, reason: str | None = None) -> bool:
        """Abandon a claimed task, making it available again. Returns True if successful."""
        rows = self.db.conn.execute(
            """UPDATE work SET status = 'abandoned', claimer = NULL,
                expires_at = NULL, completed_at = NULL, hmac = NULL, result = ?
            WHERE task_id = ? AND claimer = ? AND status = 'claimed'""",
            (reason, task_id, agent_id),
        )
        return bool(rows.rowcount > 0)

    def vacuum_expired(
        self,
        completed_age_hours: int | None = None,
        abandoned_age_hours: int | None = None,
    ) -> dict[str, int]:
        """Delete old completed and abandoned work claims.

        Returns counts of deleted rows per status.
        """
        counts: dict[str, int] = {}
        conn = self.db.conn

        if completed_age_hours is not None and completed_age_hours > 0:
            result = conn.execute(
                """DELETE FROM work
                WHERE status = 'completed'
                  AND completed_at IS NOT NULL
                  AND completed_at < datetime('now', ? || ' hours')""",
                (str(-completed_age_hours),),
            )
            counts[f"completed_older_than_{completed_age_hours}h"] = result.rowcount

        if abandoned_age_hours is not None and abandoned_age_hours > 0:
            result = conn.execute(
                """DELETE FROM work
                WHERE status = 'abandoned'
                  AND claimed_at IS NOT NULL
                  AND claimed_at < datetime('now', ? || ' hours')""",
                (str(-abandoned_age_hours),),
            )
            counts[f"abandoned_older_than_{abandoned_age_hours}h"] = result.rowcount

        return counts

    def list_available(self) -> list[WorkClaim]:
        """List all tasks available for claiming (including expired claims)."""
        rows = self.db.conn.execute(
            """SELECT * FROM work
            WHERE status = 'available'
                OR (status IN ('claimed', 'abandoned')
                    AND (claimer IS NULL OR expires_at < datetime('now')))
            ORDER BY task_id"""
        ).fetchall()
        return [_row_to_work_claim(r) for r in rows]

    def list_claimed(self, agent_id: str | None = None) -> list[WorkClaim]:
        """List claimed tasks, optionally filtered by agent."""
        if agent_id:
            rows = self.db.conn.execute(
                """SELECT * FROM work
                WHERE claimer = ? AND status = 'claimed'
                    AND expires_at >= datetime('now')
                ORDER BY task_id""",
                (agent_id,),
            ).fetchall()
        else:
            rows = self.db.conn.execute(
                """SELECT * FROM work
                WHERE status = 'claimed' AND expires_at >= datetime('now')
                ORDER BY task_id"""
            ).fetchall()
        return [_row_to_work_claim(r) for r in rows]

    def list_expired(self) -> list[WorkClaim]:
        """List claimed tasks whose claim has expired (expires_at < now, still claimed).

        Useful for detecting leaked claims where the claiming agent crashed
        without abandoning or completing the task.
        """
        rows = self.db.conn.execute(
            """SELECT * FROM work
            WHERE status = 'claimed' AND expires_at < datetime('now')
            ORDER BY task_id"""
        ).fetchall()
        return [_row_to_work_claim(r) for r in rows]

    def list_all(self) -> list[WorkClaim]:
        """List all work items regardless of status."""
        rows = self.db.conn.execute("SELECT * FROM work ORDER BY task_id").fetchall()
        return [_row_to_work_claim(r) for r in rows]

    def get(self, task_id: str) -> WorkClaim | None:
        row = self.db.conn.execute(
            "SELECT * FROM work WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_work_claim(row)

    def reopen_warnings_for_task(
        self,
        workspace_task_id: str,
        *,
        previous_state: str,
        new_state: str,
        actor_id: str | None = None,
        coordination_ids: Iterable[str] = (),
        recent_hours: int = 6,
        now: datetime | None = None,
    ) -> list[WorkReopenWarning]:
        """Return advisory warnings for reopening a workspace task.

        Checks both the local cascade key and any upstream coordination keys
        attached to the task metadata.
        """
        task_keys = [f"cascade:task:{workspace_task_id}"]
        task_keys.extend(key for key in coordination_ids if key)

        warnings: list[WorkReopenWarning] = []
        seen: set[str] = set()
        for task_key in task_keys:
            if task_key in seen:
                continue
            seen.add(task_key)
            claim = self.get(task_key)
            if claim is None:
                continue
            if not should_warn_on_task_reopen(
                claim,
                previous_state=previous_state,
                new_state=new_state,
                actor_id=actor_id,
                now=now,
                recent_hours=recent_hours,
            ):
                continue
            warnings.append(
                WorkReopenWarning(
                    task_id=claim.task_id,
                    status=claim.status,
                    claimer=claim.claimer,
                    event_at=claim.completed_at or claim.claimed_at,
                    expires_at=claim.expires_at,
                    result=claim.result,
                )
            )
        return warnings


def _row_to_work_claim(row: Any) -> WorkClaim:
    """Convert a sqlite3.Row to a WorkClaim dataclass."""
    return WorkClaim(
        task_id=row["task_id"],
        claimer=row["claimer"],
        epoch=row["epoch"],
        claimed_at=datetime.fromisoformat(row["claimed_at"])
        if row["claimed_at"]
        else None,
        expires_at=datetime.fromisoformat(row["expires_at"])
        if row["expires_at"]
        else None,
        status=row["status"],
        result=row["result"],
        metadata=row["metadata"],
        completed_at=datetime.fromisoformat(row["completed_at"])
        if "completed_at" in row.keys() and row["completed_at"]
        else None,
        hmac=row["hmac"] if "hmac" in row.keys() else None,
        verified=False,
    )
