"""Owner-only file writes for mail and message stores.

Synced mail, drafts, outbox messages and sync state contain private
correspondence. ``Path.write_text`` creates files with the process umask
(typically 0644), so any local user who can traverse the workspace could read
them. These helpers create files as 0600 and tighten files that already exist.
"""

from __future__ import annotations

import os
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
    """Write ``text`` to ``path`` (UTF-8) with owner-only permissions."""
    fd = open_private(path)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
