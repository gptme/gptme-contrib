"""Garbage-collect orphaned Chroma persist-dir segment folders.

Each ``delete_collection`` / recreate leaves UUID-named HNSW directories on
disk that are no longer listed in ``chroma.sqlite3``. They are not used by
search or index, but they accumulate (Bob's ambient-memory persist dir had 42
of them after a same-model recreate loop).

Only UUID-named directories whose names are absent from both ``segments.id``
and ``collections.id`` are candidates. ``chroma.sqlite3`` and live segment
dirs are never touched.

``--apply`` takes an exclusive writer lock (the same lock Indexer holds
around collection writes). If an indexer is mid-write, apply refuses rather
than deleting a segment dir Chroma created but has not yet catalogued.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

_UUID_DIR = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_WRITER_LOCK_NAME = ".gptme-rag-writer.lock"


class ChromaCatalogError(RuntimeError):
    """chroma.sqlite3 is unreadable; GC must not report a clean persist dir."""


@contextmanager
def index_writer_lock(persist_dir: Path, *, blocking: bool = True) -> Iterator[None]:
    """Exclusive lock around Chroma persist-dir writes.

    Indexer holds this while adding/deleting/recreating collections. GC
    ``apply`` takes it non-blocking and refuses if a writer is active — Chroma
    creates a segment directory before inserting its id into sqlite, so a GC
    snapshot taken in that window would treat a live dir as an orphan.
    """
    persist_dir = Path(persist_dir)
    persist_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(persist_dir / _WRITER_LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o644)
    flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"index writer is active in {persist_dir}; refusing to apply GC"
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _live_chroma_ids(db: Path) -> set[str]:
    """IDs that still belong to the current Chroma catalog.

    Connect by filesystem path (not a ``file:`` URI) so a persist dir whose
    path contains ``?`` is not parsed as a sqlite query string.
    """
    con = sqlite3.connect(str(db))
    try:
        con.execute("PRAGMA query_only=ON")
        tables = {
            row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        ids: set[str] = set()
        if "segments" in tables:
            ids.update(row[0] for row in con.execute("SELECT id FROM segments") if row[0])
        if "collections" in tables:
            ids.update(row[0] for row in con.execute("SELECT id FROM collections") if row[0])
        return ids
    finally:
        con.close()


def _collect_orphans(persist_dir: Path, live_ids: set[str]) -> list[Path]:
    orphans: list[Path] = []
    for child in persist_dir.iterdir():
        if not child.is_dir():
            continue
        if not _UUID_DIR.fullmatch(child.name):
            continue
        if child.name in live_ids:
            continue
        orphans.append(child)
    return orphans


def _delete_orphans(orphans: list[Path]) -> list[Path]:
    removed: list[Path] = []
    for path in orphans:
        try:
            shutil.rmtree(path)
        except OSError as exc:
            logger.warning("Failed to remove %s: %s", path, exc)
            continue
        logger.info("Removed orphan segment dir %s", path)
        removed.append(path)
    return removed


def gc_orphan_segment_dirs(persist_dir: Path, *, apply: bool = False) -> list[Path]:
    """Return orphan UUID dirs; delete them when ``apply`` is True.

    Dry-run (``apply=False``) is the default. Raises ``ChromaCatalogError`` if
    the sqlite catalog cannot be read, rather than pretending the dir is clean.
    ``apply=True`` raises ``RuntimeError`` if an index writer currently holds
    the persist-dir lock.
    """
    persist_dir = Path(persist_dir)
    db = persist_dir / "chroma.sqlite3"
    if not persist_dir.is_dir() or not db.is_file():
        return []

    try:
        if apply:
            with index_writer_lock(persist_dir, blocking=False):
                orphans = _collect_orphans(persist_dir, _live_chroma_ids(db))
                return _delete_orphans(orphans)
        return _collect_orphans(persist_dir, _live_chroma_ids(db))
    except sqlite3.Error as exc:
        raise ChromaCatalogError(f"Cannot read {db}: {exc}") from exc
