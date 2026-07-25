"""Read-many, TTL-keyed fact bus for concurrent agent knowledge sharing.

Unlike work claims (exclusive locks), facts are broadcast: any number of
sessions can query a fact simultaneously. The primary use case is preventing
convergent investigation — before spending budget to determine "is X done?",
a session queries whether a sibling already resolved that fact.

Typical fact keys:
  cascade:supply-verdict:2026-07-25     "drained"
  idea:821:status                       "claimed by 2fac"
  github:gptme/gptme#3355:merged       "true"
  task:aw-android-release:state        "waiting — Erik must tag v0.14.0"
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from gptme_coordination.db import CoordinationDB

DEFAULT_TTL_MINUTES = 60


@dataclass
class Fact:
    fact_key: str
    value: str
    session_id: str | None
    created_at: float
    expires_at: float

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at


class FactBus:
    """TTL-keyed broadcast fact store for inter-session knowledge sharing."""

    def __init__(self, db: CoordinationDB):
        self.db = db

    def publish(
        self,
        fact_key: str,
        value: str,
        session_id: str | None = None,
        ttl_minutes: int = DEFAULT_TTL_MINUTES,
    ) -> Fact:
        """Publish or overwrite a fact. Upserts on fact_key conflict."""
        now = time.time()
        expires_at = now + ttl_minutes * 60
        self.db.conn.execute(
            """INSERT INTO fact_bus (fact_key, value, session_id, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(fact_key) DO UPDATE SET
                   value = excluded.value,
                   session_id = excluded.session_id,
                   created_at = excluded.created_at,
                   expires_at = excluded.expires_at""",
            (fact_key, value, session_id, now, expires_at),
        )
        return Fact(
            fact_key=fact_key,
            value=value,
            session_id=session_id,
            created_at=now,
            expires_at=expires_at,
        )

    def query(self, fact_key: str) -> Fact | None:
        """Return the non-expired fact for the given key, or None if absent/expired."""
        row = self.db.conn.execute(
            "SELECT * FROM fact_bus WHERE fact_key = ? AND expires_at > ?",
            (fact_key, time.time()),
        ).fetchone()
        return _row_to_fact(row) if row is not None else None

    def list_facts(self, prefix: str | None = None) -> list[Fact]:
        """Return all non-expired facts, optionally filtered by key prefix."""
        now = time.time()
        if prefix:
            # Escape LIKE wildcards so a literal prefix containing % or _ works correctly
            escaped = (
                prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            rows = self.db.conn.execute(
                "SELECT * FROM fact_bus WHERE expires_at > ? AND fact_key LIKE ? ESCAPE '\\'"
                " ORDER BY fact_key",
                (now, f"{escaped}%"),
            ).fetchall()
        else:
            rows = self.db.conn.execute(
                "SELECT * FROM fact_bus WHERE expires_at > ? ORDER BY fact_key",
                (now,),
            ).fetchall()
        return [_row_to_fact(r) for r in rows]

    def purge_expired(self) -> int:
        """Delete expired facts; returns count removed."""
        cursor = self.db.conn.execute(
            "DELETE FROM fact_bus WHERE expires_at <= ?",
            (time.time(),),
        )
        return cursor.rowcount


def _row_to_fact(row: Any) -> Fact:
    return Fact(
        fact_key=row["fact_key"],
        value=row["value"],
        session_id=row["session_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )
