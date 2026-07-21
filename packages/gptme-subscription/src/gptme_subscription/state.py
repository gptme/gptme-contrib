"""Concurrency-safe persistence for small subscription state files."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None  # type: ignore[assignment]

try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - non-Windows
    _msvcrt = None  # type: ignore[assignment]


@contextmanager
def locked_state_file(path: Path) -> Iterator[None]:
    """Serialize a complete read-modify-write transaction for *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    # Binary append: no encoding argument; both fcntl and msvcrt only need the fd.
    with lock_path.open("ab") as lock_file:
        if _fcntl is not None:
            _fcntl.flock(lock_file, _fcntl.LOCK_EX)
        elif _msvcrt is not None:  # pragma: no cover - Windows only
            # locking() needs at least one byte at position 0
            if lock_file.tell() == 0:
                lock_file.write(b"\x00")
                lock_file.flush()
            lock_file.seek(0)
            _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if _fcntl is not None:
                _fcntl.flock(lock_file, _fcntl.LOCK_UN)
            elif _msvcrt is not None:  # pragma: no cover - Windows only
                lock_file.seek(0)
                _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_UNLCK, 1)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write *text* via a same-directory temporary file and atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        try:
            os.fchmod(fd, path.stat().st_mode & 0o777)
        except FileNotFoundError:
            # New file: mirror the process umask so permissions match what
            # Path.write_text() would have produced on first creation.
            current_umask = os.umask(0)
            os.umask(current_umask)  # restore immediately
            os.fchmod(fd, 0o666 & ~current_umask)
        with os.fdopen(fd, "w", encoding=encoding) as temp_file:
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
