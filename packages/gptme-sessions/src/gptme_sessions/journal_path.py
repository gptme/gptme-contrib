"""Atomically pre-allocate a session journal path before a session is dispatched.

Two concurrent launches can share the same short session label, so a journal
basename is not unique on its own. This module reserves the path with ``O_EXCL``
semantics and records a run-owned reservation file, so:

- a second run with a colliding label moves to a UUID-suffixed candidate instead
  of opening the first run's journal;
- a re-dispatched run (same run id) reuses the exact path it already owns;
- an existing unowned journal is never adopted, so historical files cannot be
  silently overwritten by a new session.

The journal is created empty rather than pre-filled: the model writes it, and a
placeholder body would have to be replaced anyway.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
from datetime import date as calendar_date
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "JournalReservationError",
    "allocate_journal_path",
    "main",
]

#: Run types that keep the long-standing ``autonomous-session-<label>`` stem.
#: They are session kinds, not agent identities, so the mapping stays neutral.
AUTONOMOUS_STEM_RUN_TYPES = frozenset({"autonomous", "cleanup", "self-review"})


class JournalReservationError(RuntimeError):
    """Reservation metadata is corrupt or disagrees with the requested run."""


def try_create(path: Path) -> bool:
    """Create an empty journal exclusively. Return whether this call created it."""
    try:
        # 'x' mode is O_CREAT | O_EXCL — atomic on POSIX filesystems.
        path.open("x").close()
        return True
    except FileExistsError:
        return False


def read_owner_run_id(owner_path: Path, path: Path) -> str:
    """Read a complete owner record, validating it before comparing run ids."""
    try:
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise JournalReservationError(f"invalid owner record {owner_path}: {exc}") from exc
    if (
        not isinstance(owner, dict)
        or not isinstance(owner.get("run_id"), str)
        or not owner["run_id"]
        or not isinstance(owner.get("journal_path"), str)
    ):
        raise JournalReservationError(
            f"invalid owner record {owner_path}: expected run/path object"
        )
    if owner["journal_path"] != str(path):
        raise JournalReservationError(
            f"owner record path mismatch for {path}: {owner['journal_path']!r}"
        )
    run_id: str = owner["run_id"]
    return run_id


def reserve_candidate(path: Path, owner_path: Path, run_id: str) -> bool:
    """Reserve *path* for *run_id*, returning False on a normal collision.

    A persistent POSIX flock file serializes the owner check, journal creation and
    record publication across both threads and processes. Never unlink the lock:
    waiters must keep using the same inode. Process exit releases a crashed lock.

    Publish ownership only AFTER exclusive journal creation. A crash before that
    publication leaves an unowned journal, which is conservatively skipped on
    retry; a crash after publication leaves a complete, reusable reservation.
    """
    owner_path.parent.mkdir(parents=True, exist_ok=True)
    with owner_path.with_suffix(".lock").open("a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        if owner_path.exists():
            if read_owner_run_id(owner_path, path) != run_id:
                return False
            if path.is_symlink() or not path.is_file():
                raise JournalReservationError(f"owned journal path is not a regular file: {path}")
            return True

        if not try_create(path):
            # Historical/unowned files are never adopted, including crash orphans.
            return False

        # A unique temporary file belongs to this call alone. link(2) publishes
        # the complete record without replacing any existing owner.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=owner_path.parent,
            prefix=f".{owner_path.name}.",
            delete=False,
        ) as owner_file:
            owner_tmp = Path(owner_file.name)
            try:
                json.dump({"run_id": run_id, "journal_path": str(path)}, owner_file)
                owner_file.write("\n")
                owner_file.flush()
                os.fsync(owner_file.fileno())
                os.link(owner_tmp, owner_path)
            finally:
                owner_tmp.unlink(missing_ok=True)
        return True


def journal_stem(run_type: str, label: str) -> str:
    """Return the basename stem for a run type and short session label."""
    if run_type in AUTONOMOUS_STEM_RUN_TYPES:
        return f"autonomous-session-{label}"
    return f"{run_type}-session-{label}"


def candidates_for(journal_dir: Path, run_type: str, label: str, run_id: str) -> list[Path]:
    """Return candidate journal paths, most preferred first."""
    stem = journal_stem(run_type, label)
    compact = run_id.replace("-", "")
    return [
        journal_dir / f"{stem}.md",
        journal_dir / f"{stem}-{compact[:4]}.md",
        journal_dir / f"{stem}-{compact[:8]}.md",
    ]


def allocate_journal_path(
    journal_dir: Path,
    run_type: str,
    label: str,
    run_id: str,
    date: str | None = None,
    reservation_dir: Path | None = None,
) -> Path:
    """Allocate and reserve an empty journal file, returned as an absolute path.

    ``date`` defaults to today (UTC) and names the ``YYYY-MM-DD`` subdirectory.
    ``reservation_dir`` defaults to
    ``<journal_dir>/../state/session-journal-reservations/<journal-dir-name>``.
    """
    for name, value in (("run type", run_type), ("label", label), ("run id", run_id)):
        if not isinstance(value, str) or not value or any(c in value for c in "/\\\0"):
            raise JournalReservationError(
                f"{name} must be a non-empty filename component without separators or NUL"
            )
    date_str = date if date is not None else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        parsed_date = calendar_date.fromisoformat(date_str)
    except ValueError as exc:
        raise JournalReservationError("date must be a valid YYYY-MM-DD date") from exc
    if parsed_date.isoformat() != date_str:
        raise JournalReservationError("date must be a valid YYYY-MM-DD date")
    journal_dir = Path(journal_dir).resolve()
    day_dir = journal_dir / date_str
    default_reservation = (
        journal_dir.parent / "state" / "session-journal-reservations" / journal_dir.name
    )
    owner_root = Path(reservation_dir or default_reservation).resolve()
    owner_root.mkdir(parents=True, exist_ok=True)
    # Serialize the cross-date lookup and allocation, including calls on opposite
    # sides of UTC midnight. An explicit date deliberately scopes the reservation.
    with (owner_root / ".allocation.lock").open("a") as allocation_lock:
        fcntl.flock(allocation_lock.fileno(), fcntl.LOCK_EX)
        previous_dirs = (
            sorted(owner_root.glob("????-??-??")) if date is None else [owner_root / date_str]
        )
        # Find an owned suffix before creating any newly available candidate.
        for previous in previous_dirs:
            if not previous.is_dir():
                continue
            for candidate in candidates_for(journal_dir / previous.name, run_type, label, run_id):
                owner_path = previous / f"{candidate.name}.json"
                if not owner_path.exists():
                    continue
                if read_owner_run_id(owner_path, candidate) == run_id:
                    if reserve_candidate(candidate, owner_path, run_id):
                        return candidate
        owner_dir = owner_root / date_str
        day_dir.mkdir(parents=True, exist_ok=True)
        candidates = candidates_for(day_dir, run_type, label, run_id)
        for path in candidates:
            if reserve_candidate(path, owner_dir / f"{path.name}.json", run_id):
                return path
    raise JournalReservationError(
        f"all collision-avoidance candidates taken for label={label} run={run_id}: "
        f"{[str(candidate) for candidate in candidates]}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Atomically allocate a session journal path")
    parser.add_argument("--journal-dir", required=True, help="Parent journal directory")
    parser.add_argument(
        "--run-type",
        required=True,
        help="Run type (autonomous, cleanup, self-review, monitoring, …)",
    )
    parser.add_argument("--label", required=True, help="Short session label/hash")
    parser.add_argument(
        "--uuid", required=True, help="Full session run id, used for collision suffixes"
    )
    parser.add_argument("--date", default=None, help="Date subdir (default: today UTC)")
    parser.add_argument(
        "--reservation-dir",
        default=None,
        help=(
            "Directory for durable run-owner records (default: "
            "<journal-dir>/../state/session-journal-reservations/<journal-dir-name>)"
        ),
    )
    args = parser.parse_args(argv)

    try:
        path = allocate_journal_path(
            Path(args.journal_dir),
            args.run_type,
            args.label,
            args.uuid,
            date=args.date,
            reservation_dir=(Path(args.reservation_dir) if args.reservation_dir else None),
        )
    except OSError as exc:
        print(f"ERROR: cannot create journal dir: {exc}", file=sys.stderr)
        return 1
    except JournalReservationError as exc:
        print(f"ERROR: journal reservation integrity failure: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
