"""Canonical JSONL loading.

Replaces ~20 per-file ``load_jsonl``/``_iter_jsonl`` implementations whose
behavior had drifted across four axes: missing-file handling, corrupt-line
handling, non-dict-row filtering, and encoding. The canonical semantics match
the de-facto majority pattern, with explicit opt-ins for the strict callers:

- Missing file: yields nothing / returns ``[]`` (pass ``must_exist=True`` to
  raise ``FileNotFoundError`` instead — data-integrity checks want a missing
  ledger to fail loudly, not read as empty).
- Blank lines: always skipped.
- Corrupt JSON lines and non-dict rows: governed by ``on_invalid`` —
  ``"skip"`` (silent, default), ``"warn"`` (``logging.warning`` per line),
  or ``"raise"`` (``ValueError`` with path and line number).
- ``.gz`` paths are opened transparently with :mod:`gzip`.
"""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

__all__ = ["iter_jsonl", "load_jsonl"]

logger = logging.getLogger(__name__)

_ON_INVALID_MODES = ("skip", "warn", "raise")


def iter_jsonl(
    path: str | Path,
    *,
    encoding: str = "utf-8",
    on_invalid: str = "skip",
    must_exist: bool = False,
) -> Iterator[dict[str, Any]]:
    """Return an iterator over dict rows from a JSONL file, one per non-blank line.

    Parameters are validated eagerly at call time (not deferred to first iteration),
    so ``iter_jsonl(path, on_invalid="typo")`` raises immediately rather than
    raising on the first ``next()`` call.
    """
    if on_invalid not in _ON_INVALID_MODES:
        raise ValueError(
            f"on_invalid must be one of {_ON_INVALID_MODES}, got {on_invalid!r}"
        )
    path = Path(path)
    if not path.exists():
        if must_exist:
            raise FileNotFoundError(f"JSONL file not found: {path}")
        return iter(())
    return _iter_jsonl_rows(path, encoding=encoding, on_invalid=on_invalid)


def _iter_jsonl_rows(
    path: Path,
    *,
    encoding: str,
    on_invalid: str,
) -> Iterator[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding=encoding) as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            problem: str | None = None
            row: Any = None
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                problem = f"invalid JSON: {e}"
            if problem is None and not isinstance(row, dict):
                problem = f"non-dict row of type {type(row).__name__}"
            if problem is not None:
                if on_invalid == "raise":
                    raise ValueError(f"{path}:{lineno}: {problem}")
                if on_invalid == "warn":
                    logger.warning("%s:%d: %s (skipping line)", path, lineno, problem)
                continue
            yield row


def load_jsonl(
    path: str | Path,
    *,
    encoding: str = "utf-8",
    on_invalid: str = "skip",
    must_exist: bool = False,
    tail: int | None = None,
) -> list[dict[str, Any]]:
    """Load dict rows from a JSONL file into a list.

    ``tail=N`` returns only the last N valid rows — useful for append-only
    ledgers where only recent entries matter.
    """
    rows = list(
        iter_jsonl(
            path, encoding=encoding, on_invalid=on_invalid, must_exist=must_exist
        )
    )
    if tail is not None:
        return rows[-tail:] if tail > 0 else []
    return rows
