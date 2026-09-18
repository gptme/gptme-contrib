"""Write-side helpers: the same never-shorten rule every writer must obey.

A single writer that shortens a block re-opens an arm that another detector
deliberately closed. Centralizing the rule here is the point: the shell writer
and the Python writers cannot drift.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from .contract import format_timestamp, is_canonical_timestamp, parse_until


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Serialize writers to ``path`` via an advisory POSIX lock on a sibling file.

    Without this, two concurrent writers can both read the same stale existing
    deadline, both decide their own value should win, and the later `os.replace`
    silently overwrites the other — the never-shorten guarantee only holds
    within a single writer otherwise. POSIX-only (`fcntl`), matching the shell
    writer's own `flock`-based bash implementation.
    """
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def write_block(path: Path | str, until: datetime) -> Path:
    """Write a block deadline, never shortening an existing canonical one.

    Returns the path. If the file already holds a canonical, later deadline, the
    file is left untouched and the existing deadline wins. A non-canonical (or
    unparseable) existing value is treated as unknown and overwritten.

    The read-compare-write is serialized by an advisory lock, and the write
    itself is atomic (temp file + rename) so a concurrent reader never observes
    a truncated or partial timestamp.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_raw = format_timestamp(until)

    with _locked(path):
        if path.is_file():
            try:
                existing_raw = path.read_text()
            except OSError:
                existing_raw = ""
            if is_canonical_timestamp(existing_raw):
                existing = parse_until(existing_raw)
                new = parse_until(new_raw)
                if existing is not None and new is not None and existing > new:
                    return path  # existing block outlasts the new one; keep it

        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w") as tmp_file:
                tmp_file.write(new_raw + "\n")
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    return path


def clear_block(path: Path | str) -> bool:
    """Remove a block file. Returns True if a file was removed."""
    path = Path(path)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def read_block_until(path: Path | str) -> datetime | None:
    """Read a deadline; ``None`` when absent, unreadable, or garbled."""
    path = Path(path)
    try:
        raw = path.read_text()
    except OSError:
        return None
    return parse_until(raw)
