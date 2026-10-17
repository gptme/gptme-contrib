"""Immutable experiment run-reservation and whole-run admission gate.

A reservation couples a pinned no-training route/model to an experiment's
session and labeling budgets. The gate must be satisfied before any provider
call is placed. Failed and timed-out sends count against the budget because
the call attempt is reserved before the subprocess is spawned.

No dollar ceiling is computed here — the caller supplies a ``spend_authority``
reference (e.g. an approved issue number) that must be verified externally
before activation. Tests use synthetic values; no paid calls happen here.

Activation prerequisites (must all be satisfied before live use):
- ``route``: a real, verified no-training route (provider policy confirmed
  for private workspace text — intent queries and lesson catalog descriptions)
- ``model``: exact model string with confirmed pricing (p/token); cannot be
  a placeholder
- ``spend_authority``: a durable approval reference (issue/PR/decision record)
  that names the authorized ceiling; do not infer a dollar amount from
  average usage — obtain it from actual spend approval
- ``max_sessions_per_arm_per_runtime`` × ``arms`` × ``runtimes`` must not
  exceed the approved ceiling at the verified route/model price
"""

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

_VERSION = 1

# Fields bound into the reservation hash (any change invalidates the
# reservation and forces the gate to refuse).
_BOUND_FIELDS = (
    "experiment_id",
    "version",
    "route",
    "model",
    "runtimes",
    "arms",
    "max_sessions_per_arm_per_runtime",
    "max_attempts_per_session",
    "labeling",
)


def _labeling_payload(labeling: dict[str, Any]) -> dict[str, Any]:
    return {
        "max_items_per_cell": int(labeling["max_items_per_cell"]),
        "max_labels_per_item": int(labeling["max_labels_per_item"]),
        "adjudication_possible": bool(labeling["adjudication_possible"]),
    }


def compute_reservation_hash(data: dict[str, Any]) -> str:
    """SHA-256 of the bound experiment fields; stable across JSON serialisation."""
    payload: dict[str, Any] = {
        "experiment_id": str(data["experiment_id"]),
        "version": int(data["version"]),
        "route": str(data["route"]),
        "model": str(data["model"]),
        "runtimes": sorted(str(r) for r in data["runtimes"]),
        "arms": sorted(str(a) for a in data["arms"]),
        "max_sessions_per_arm_per_runtime": int(
            data["max_sessions_per_arm_per_runtime"]
        ),
        "max_attempts_per_session": int(data["max_attempts_per_session"]),
        "labeling": _labeling_payload(data["labeling"]),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def validate_reservation(data: Any) -> dict[str, Any]:
    """Return a normalised reservation dict or raise ValueError."""
    if not isinstance(data, dict):
        raise ValueError("reservation must be a JSON object")
    if data.get("version") != _VERSION:
        raise ValueError(f"unsupported reservation version: {data.get('version')!r}")
    for key in ("experiment_id", "route", "model", "spend_authority", "created_at"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"reservation missing required string field: {key!r}")
    runtimes = data.get("runtimes")
    if not isinstance(runtimes, list) or not runtimes:
        raise ValueError("reservation.runtimes must be a non-empty list")
    arms = data.get("arms")
    if not isinstance(arms, list) or not arms:
        raise ValueError("reservation.arms must be a non-empty list")
    max_sessions = data.get("max_sessions_per_arm_per_runtime")
    if type(max_sessions) is not int or max_sessions < 1:
        raise ValueError(
            "reservation.max_sessions_per_arm_per_runtime must be a positive int"
        )
    max_attempts = data.get("max_attempts_per_session")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("reservation.max_attempts_per_session must be a positive int")
    labeling = data.get("labeling")
    if not isinstance(labeling, dict):
        raise ValueError("reservation.labeling must be a JSON object")
    for key in ("max_items_per_cell", "max_labels_per_item"):
        if type(labeling.get(key)) is not int or labeling[key] < 1:
            raise ValueError(f"reservation.labeling.{key} must be a positive int")
    if not isinstance(labeling.get("adjudication_possible"), bool):
        raise ValueError("reservation.labeling.adjudication_possible must be a bool")
    stored_hash = data.get("reservation_hash")
    if not isinstance(stored_hash, str) or not stored_hash:
        raise ValueError("reservation missing reservation_hash")
    result: dict[str, Any] = {
        "version": _VERSION,
        "experiment_id": data["experiment_id"],
        "route": data["route"],
        "model": data["model"],
        "runtimes": [str(r) for r in runtimes],
        "arms": [str(a) for a in arms],
        "max_sessions_per_arm_per_runtime": max_sessions,
        "max_attempts_per_session": max_attempts,
        "labeling": _labeling_payload(labeling),
        "spend_authority": data["spend_authority"],
        "created_at": data["created_at"],
        "reservation_hash": stored_hash,
    }
    expected_hash = compute_reservation_hash(result)
    if stored_hash != expected_hash:
        raise ValueError(
            f"reservation_hash mismatch: stored={stored_hash[:12]}... "
            f"computed={expected_hash[:12]}..."
        )
    return result


def load_reservation(path: Path) -> dict[str, Any] | None:
    """Load and validate a reservation JSON; return None if missing or invalid."""
    try:
        with path.open("rb") as stream:
            data = json.loads(stream.read())
        return validate_reservation(data)
    except (OSError, ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# SQLite gate — reuses the selector's own state DB
# ---------------------------------------------------------------------------

_CREATE_ENROLLMENTS = """\
CREATE TABLE IF NOT EXISTS experiment_enrollments (
    experiment_id TEXT, runtime TEXT, session_id TEXT,
    PRIMARY KEY (experiment_id, runtime, session_id))"""

_CREATE_LABELING = """\
CREATE TABLE IF NOT EXISTS experiment_labeling (
    experiment_id TEXT, runtime TEXT, arm TEXT, items_used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (experiment_id, runtime, arm))"""


def _ensure_tables(connection: sqlite3.Connection) -> None:
    for stmt in (_CREATE_ENROLLMENTS, _CREATE_LABELING):
        connection.execute(stmt)


def check_session_admission(
    reservation: dict[str, Any],
    connection: sqlite3.Connection,
    *,
    runtime: str,
    session_id: str,
) -> str | None:
    """Atomically enroll a session; return a fallback reason string or None.

    Called inside an IMMEDIATE transaction already held by the caller.
    On success the session is recorded as enrolled for this experiment;
    on failure the transaction should be rolled back or the row discarded.

    Per-arm accounting is not performed here — the total budget per runtime
    across all arms is ``max_sessions_per_arm_per_runtime × len(arms)``.
    """
    experiment_id = reservation["experiment_id"]
    total_cap: int = reservation["max_sessions_per_arm_per_runtime"] * len(
        reservation["arms"]
    )
    _ensure_tables(connection)
    existing = connection.execute(
        "SELECT 1 FROM experiment_enrollments WHERE experiment_id=? AND runtime=? AND session_id=?",
        (experiment_id, runtime, session_id),
    ).fetchone()
    if existing:
        return None  # already enrolled — this session is within budget
    enrolled_count: int = connection.execute(
        "SELECT COUNT(*) FROM experiment_enrollments WHERE experiment_id=? AND runtime=?",
        (experiment_id, runtime),
    ).fetchone()[0]
    if enrolled_count >= total_cap:
        return "reservation_exhausted"
    connection.execute(
        "INSERT INTO experiment_enrollments VALUES (?, ?, ?)",
        (experiment_id, runtime, session_id),
    )
    return None


def check_labeling_admission(
    reservation: dict[str, Any],
    state_dir: Path,
    *,
    runtime: str,
    arm: str,
    n_items: int = 1,
    deadline_seconds: float = 1.0,
) -> tuple[bool, str]:
    """Atomically check and reserve n_items labeling slots; returns (ok, reason).

    Returns (True, "admitted") or (False, "<reason>").
    """
    experiment_id = reservation["experiment_id"]
    cap: int = reservation["labeling"]["max_items_per_cell"]
    if arm not in reservation["arms"]:
        return False, "labeling_unknown_arm"
    if runtime not in reservation["runtimes"]:
        return False, "labeling_unknown_runtime"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        database_path = state_dir / "selector.sqlite3"
        connection = sqlite3.connect(database_path, timeout=deadline_seconds)
        os.chmod(database_path, 0o600)
        try:
            connection.execute(f"PRAGMA busy_timeout={int(deadline_seconds * 1000)}")
            _ensure_tables(connection)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT items_used FROM experiment_labeling WHERE experiment_id=? AND runtime=? AND arm=?",
                (experiment_id, runtime, arm),
            ).fetchone()
            current: int = row[0] if row else 0
            if current + n_items > cap:
                connection.rollback()
                return False, "labeling_exhausted"
            if row:
                connection.execute(
                    "UPDATE experiment_labeling SET items_used=? WHERE experiment_id=? AND runtime=? AND arm=?",
                    (current + n_items, experiment_id, runtime, arm),
                )
            else:
                connection.execute(
                    "INSERT INTO experiment_labeling VALUES (?, ?, ?, ?)",
                    (experiment_id, runtime, arm, n_items),
                )
            connection.commit()
            return True, "admitted"
        finally:
            connection.close()
    except (sqlite3.Error, OSError):
        return False, "labeling_state_unavailable"
