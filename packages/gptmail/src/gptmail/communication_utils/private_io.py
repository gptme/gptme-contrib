"""Owner-only file writes for mail and message stores.

Synced mail, drafts, outbox messages and sync state contain private
correspondence. ``Path.write_text`` creates files with the process umask
(typically 0644), so any local user who can traverse the workspace could read
them. These helpers create files as 0600 and tighten files that already exist.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

PRIVATE_FILE_MODE = 0o600


def open_private(path: str | Path, flags: int = os.O_WRONLY | os.O_CREAT | os.O_TRUNC) -> int:
    """Open ``path`` with ``flags``, creating it 0600 and tightening an existing file."""
    fd = os.open(str(path), flags, PRIVATE_FILE_MODE)
    try:
        os.fchmod(fd, PRIVATE_FILE_MODE)
    except BaseException:
        os.close(fd)
        raise
    return fd


def write_private_text(path: str | Path, text: str) -> None:
    """Atomically write ``text`` to ``path`` (UTF-8) with owner-only permissions.

    Readers never see a partial file, and an interrupted write leaves any
    existing file as it was.
    """
    path = Path(path)
    # Short, non-``.md`` name: invisible to message globs, no length overflow.
    temp = path.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        with os.fdopen(open_private(temp), "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
