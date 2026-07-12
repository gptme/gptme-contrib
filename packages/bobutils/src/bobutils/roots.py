"""Canonical repo-root resolver.

Replaces ~9 per-script ``find_repo_root`` / ``repo_root`` / ``_resolve_repo_root``
implementations that all did the same ``git rev-parse --show-toplevel`` call but
varied in error handling and whether they accepted a ``cwd`` argument.

**Semantics**:

- Raises ``RuntimeError`` on failure (no silent fallbacks — silent fallbacks hid
  a real bug in ``scripts/cross-repo-supply-probe.py`` where a missing ``.git``
  made it return the filesystem root).
- ``cwd`` defaults to ``Path.cwd()`` when *None*, mirroring how ``git`` itself
  resolves the working directory.
- Returns the *worktree* root, not the shared-state root.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = ["find_repo_root"]


def find_repo_root(cwd: Path | None = None) -> Path:
    """Return the top-level directory of the git repo containing *cwd*.

    Args:
        cwd: Directory to start from.  Defaults to ``Path.cwd()``.

    Returns:
        Absolute ``Path`` to the repo root (as reported by git).

    Raises:
        RuntimeError: If ``cwd`` is not inside a git repository or git fails.
    """
    resolved = (cwd or Path.cwd()).resolve()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=resolved,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git executable not found on PATH") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"git rev-parse --show-toplevel failed in {resolved}: {result.stderr.strip()}"
        )
    return Path(result.stdout.strip())
