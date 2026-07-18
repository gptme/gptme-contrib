"""Concurrency-safe helpers for small run-loop state files."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows fallback
    _fcntl = None  # type: ignore[assignment]


@contextmanager
def locked_state_file(path: Path) -> Iterator[None]:
    """Hold the sidecar lock shared by all writers of *path*.

    The state file itself is replaced atomically, so its inode cannot be used as
    the lock target. A stable ``<name>.lock`` sidecar serializes the complete
    read-modify-write transaction instead.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    with lock_path.open("a", encoding="utf-8") as lock_file:
        if _fcntl is not None:
            _fcntl.flock(lock_file, _fcntl.LOCK_EX)
        try:
            yield
        finally:
            if _fcntl is not None:
                _fcntl.flock(lock_file, _fcntl.LOCK_UN)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write *text* via a same-directory temporary file and ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as temp_file:
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
