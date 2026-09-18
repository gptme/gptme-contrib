"""The block-file wire contract.

This module is deliberately policy-free: it knows how block files are *named*
and how their timestamps are *shaped and compared*, but not which arm should be
blocked. Policy (route resolution, scoped-key choice, backend rules) belongs to
the caller.

Location
--------
``<workspace>/state/backend-quota/`` by default, overridable by the caller.

Filename families
-----------------
==================  ====================================================  ===========================
Family              Pattern                                               Writer
==================  ====================================================  ===========================
Crash loop          ``{backend}-{model_safe}-crash-loop-until.txt``       autonomous-run
Daily crash loop    ``{backend}-{model_safe}-daily-crash-loop-until.txt`` autonomous-run
Quality             ``{backend}-{model_safe}-quality-blocked-until.txt``  quality regression gate
Backend-level       ``{backend}-blocked-until.txt``                       quota check
Pool-level          ``pool-{pool}-blocked-until.txt``                     pool exhaustion
OpenRouter limit    ``openrouter-{context}-daily-limit-until.txt``        autonomous-run / PM slots
OpenRouter shared   ``openrouter-daily-limit-until.txt``                  same, unscoped context
==================  ====================================================  ===========================

``model_safe(model)`` replaces ``/`` and spaces with ``-``. Callers must
canonicalize model aliases *before* deriving a filename so that, e.g., a short
alias and its full name hit the same file.

Timestamp shape
---------------
Fixed-width ISO-8601 UTC with a ``+00:00`` offset, as emitted by
``date -u ... --iso-8601=seconds``. Lexicographic comparison is valid **only**
for this exact shape.

Failure semantics
-----------------
* A reader treats an unreadable or garbled timestamp as **clear** (fail-open):
  a corrupt block file must never brick an arm.
* A writer treats a non-canonical existing timestamp as unknown and overwrites
  it. It **never shortens** an existing canonical block.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

#: Arm-level block kinds, in evaluation order, mapped to their filename suffix.
ARM_BLOCK_KINDS: dict[str, str] = {
    "crash_loop": "crash-loop-until.txt",
    "daily_crash_loop": "daily-crash-loop-until.txt",
    "quality": "quality-blocked-until.txt",
}

_OPENROUTER_SUFFIX = "daily-limit-until.txt"
_POOL_SUFFIX = "blocked-until.txt"


def model_safe(model: str) -> str:
    """Filename-safe model key: ``/`` and spaces become ``-``.

    Must stay byte-identical to the writers' derivation, or readers and writers
    silently address different files.
    """
    return model.replace("/", "-").replace(" ", "-")


def format_timestamp(dt: datetime) -> str:
    """Render a deadline in the canonical wire shape (seconds, ``+00:00``)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def is_canonical_timestamp(raw: str) -> bool:
    """True when ``raw`` is exactly the fixed-width UTC shape.

    Only canonical shapes may be compared lexicographically or trusted as a
    floor by :func:`..writers.write_block`.
    """
    stripped = raw.strip()
    if not stripped:
        return False
    try:
        parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return False
    return format_timestamp(parsed) == stripped


def parse_until(raw: str) -> datetime | None:
    """Parse a deadline; ``None`` means garbled/empty (reader fails open).

    Naive timestamps are read as UTC rather than rejected — the wire shape is
    UTC and a missing offset is a writer bug we can still interpret safely.
    """
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        ts = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def arm_block_path(state_dir: Path, backend: str, model: str, kind: str) -> Path:
    """Path of an arm-level block file. ``kind`` must be in :data:`ARM_BLOCK_KINDS`."""
    try:
        suffix = ARM_BLOCK_KINDS[kind]
    except KeyError:
        raise ValueError(
            f"unknown arm block kind {kind!r}; expected one of {sorted(ARM_BLOCK_KINDS)}"
        ) from None
    return state_dir / f"{backend}-{model_safe(model)}-{suffix}"


def backend_block_path(state_dir: Path, backend: str) -> Path:
    """Path of the backend-level block, e.g. all arms on a shared premium pool."""
    return state_dir / f"{backend}-{_POOL_SUFFIX}"


def pool_block_path(state_dir: Path, pool: str) -> Path:
    """Path of a pool-level block, shared by every route drawing from ``pool``."""
    return state_dir / f"pool-{pool}-{_POOL_SUFFIX}"


def openrouter_block_path(state_dir: Path, context: str | None = None) -> Path:
    """Path of the OpenRouter limit block for ``context``.

    ``context=None`` (or empty) is the shared/unscoped chain. The "daily" in the
    filename is **historical** — the file holds an absolute deadline for any
    limit window (daily/weekly/monthly/credits). Renaming it means updating
    every reader, so it stays.
    """
    if not context:
        return state_dir / f"openrouter-{_OPENROUTER_SUFFIX}"
    return state_dir / f"openrouter-{context.lower()}-{_OPENROUTER_SUFFIX}"
