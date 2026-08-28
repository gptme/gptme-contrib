"""Garbage-collect orphaned Chroma persist-dir segment folders.

Each ``delete_collection`` / recreate leaves UUID-named HNSW directories on
disk that are no longer listed in ``chroma.sqlite3``. They are not used by
search or index, but they accumulate (Bob's ambient-memory persist dir had 42
of them after a same-model recreate loop).

Only UUID-named directories whose names are absent from both ``segments.id``
and ``collections.id`` are candidates. ``chroma.sqlite3`` and live segment
dirs are never touched.
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_UUID_DIR = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _live_chroma_ids(db: Path) -> set[str]:
    """IDs that still belong to the current Chroma catalog."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
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


def gc_orphan_segment_dirs(persist_dir: Path, *, apply: bool = False) -> list[Path]:
    """Return orphan UUID dirs; delete them when ``apply`` is True.

    Dry-run (``apply=False``) is the default. If the sqlite catalog cannot be
    read, return an empty list rather than deleting anything.
    """
    persist_dir = Path(persist_dir)
    db = persist_dir / "chroma.sqlite3"
    if not persist_dir.is_dir() or not db.is_file():
        return []

    try:
        live_ids = _live_chroma_ids(db)
    except sqlite3.Error as exc:
        logger.warning("Cannot read %s (%s); refusing to GC", db, exc)
        return []

    orphans: list[Path] = []
    for child in persist_dir.iterdir():
        if not child.is_dir():
            continue
        if not _UUID_DIR.fullmatch(child.name):
            continue
        if child.name in live_ids:
            continue
        orphans.append(child)

    if apply:
        for path in orphans:
            shutil.rmtree(path)
            logger.info("Removed orphan segment dir %s", path)

    return orphans
