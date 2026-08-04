"""Symlink-safe atomic file writer.

Guards the clobber class: ``os.replace(tmp, symlink)`` on Linux replaces the
*symlink itself* with the temp file instead of updating the target content.
Writers that use this module resolve the symlink chain first, place the temp
file in the target's parent directory, then atomically replace the real target
— not the link — leaving all symlinks intact.

Recorded clobber classes this prevents
---------------------------------------
- Subscription-slot live symlink (``~/.claude/.credentials.json →
  .credentials.json.bob``) replaced by a regular file during a credential
  refresh, silently forking the credential state.
- Per-slot OCR cache symlinks replaced by fresh scrape outputs, breaking the
  symlink-based indirection that lets multiple consumers share one file.

Usage
-----
::

    from bobutils.safe_write import safe_write, safe_write_bytes

    # Text write — resolves symlinks, writes atomically to the real target
    safe_write(Path("~/.claude/.credentials.json"), json_text, mode=0o600)

    # Binary write
    safe_write_bytes(cache_symlink, png_bytes, mode=0o644)

Both functions are stdlib-only and have no external dependencies.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

__all__ = ["safe_write", "safe_write_bytes"]


def safe_write(path: Path | str, data: str, *, mode: int = 0o644) -> None:
    """Atomically write *data* (text, UTF-8) to *path*, preserving symlinks.

    If *path* is a symlink (or a chain of symlinks), the write lands on the
    resolved real target so all links in the chain are preserved.  A temp
    file is created in the same directory as the resolved target (ensuring
    same-filesystem placement for ``os.replace``) and atomically renamed over
    the target.

    Args:
        path:  Destination path (may be a symlink or symlink chain).
        data:  Text content to write, encoded as UTF-8.
        mode:  Permission bits applied when creating a *new* file.  When the
               target already exists, its current permissions are preserved —
               this prevents accidentally widening restrictive modes (e.g.
               ``0o600`` credential files).

    Raises:
        OSError:  If the write or rename fails (e.g. wrong permissions, full
                  filesystem).
        FileNotFoundError:  If *path* is a broken symlink and the target's
                            parent directory does not exist.
    """
    _atomic_write(Path(path), data.encode("utf-8"), mode=mode)


def safe_write_bytes(path: Path | str, data: bytes, *, mode: int = 0o644) -> None:
    """Atomically write *data* (bytes) to *path*, preserving symlinks.

    Args:
        path:  Destination path (may be a symlink or symlink chain).
        data:  Raw bytes to write.
        mode:  Permission bits applied when creating a *new* file.  Existing
               file permissions are preserved (see :func:`safe_write`).

    Raises:
        OSError:  If the write or rename fails.
        FileNotFoundError:  If *path* is a broken symlink and the target's
                            parent directory does not exist.
    """
    _atomic_write(Path(path), data, mode=mode)


def _atomic_write(path: Path, data: bytes, *, mode: int) -> None:
    """Core implementation: resolve → temp → replace at real target.

    Steps
    -----
    1. Resolve any symlink chain to the real file path.
       - If the target exists (normal case): ``resolve(strict=True)`` follows
         every link and returns the absolute real path.
       - If the target does not exist yet (new file): fall back to
         ``resolve(strict=False)`` which collapses ``..`` and resolves as many
         links as possible; this lets us create the file at the right location.
    2. Create a temp file in the **same directory** as the real target.
       Same-directory placement is required for ``os.replace`` to be atomic
       on POSIX (``rename(2)`` only works within the same filesystem, and
       temp files in ``/tmp`` could live on a different mount point).
    3. Write data and atomically rename temp → real target.
    """
    # Step 1: resolve symlinks
    try:
        real = path.resolve(strict=True)
    except (FileNotFoundError, OSError):
        # New file or broken symlink — resolve without requiring target to exist
        real = path.resolve(strict=False)

    # Step 2: temp file in the same directory as the real target.
    # parents=False: if the direct parent doesn't exist (e.g. broken symlink
    # pointing into a missing subtree), raise FileNotFoundError as documented.
    real.parent.mkdir(parents=False, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=real.parent)
    try:
        # Preserve existing permissions; use caller-supplied mode only for new
        # files.  os.replace() transplants the temp inode, so whatever mode the
        # temp file carries becomes the mode of the resulting file — we must set
        # it explicitly or existing restrictive modes (e.g. 0o600 credential
        # files) would be silently widened to the default 0o644.
        effective_mode = mode
        try:
            effective_mode = stat.S_IMODE(os.stat(real).st_mode)
        except OSError:
            pass  # Target doesn't exist yet — use caller-supplied mode
        os.chmod(tmp_name, effective_mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        # Step 3: atomic replace — lands on real target, not the symlink
        os.replace(tmp_name, real)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
