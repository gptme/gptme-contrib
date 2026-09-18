"""Write-side helpers: the same never-shorten rule every writer must obey.

A single writer that shortens a block re-opens an arm that another detector
deliberately closed. Centralizing the rule here is the point: the shell writer
and the Python writers cannot drift.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .contract import format_timestamp, is_canonical_timestamp, parse_until


def write_block(path: Path | str, until: datetime) -> Path:
    """Write a block deadline, never shortening an existing canonical one.

    Returns the path. If the file already holds a canonical, later deadline, the
    file is left untouched and the existing deadline wins. A non-canonical (or
    unparseable) existing value is treated as unknown and overwritten.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_raw = format_timestamp(until)

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

    path.write_text(new_raw + "\n")
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
