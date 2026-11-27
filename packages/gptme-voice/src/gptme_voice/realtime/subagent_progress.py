"""Bounded live progress for the voice subagent_status tool.

Child stdout is not a model transcript and is not private reasoning. The
realtime agent needs enough retained output to tell startup log noise from
useful work, silence after work, and a completed or failed prior-leg task.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

# Keep the status payload small enough for a live voice turn.
MAX_PROGRESS_LINES = 8
MAX_PROGRESS_CHARS = 800
MAX_PROGRESS_LINE_CHARS = 200
# Matches the fast-mode partial-answer prompt target. Quiet longer than this
# after meaningful output is "silent", not "still starting".
SILENCE_SECONDS = 8.0

# Returncodes from GNU timeout / SIGTERM wrapper. Duplicated here so this
# module stays importable without the ToolBridge (timeout work lives there).
_TIMEOUT_RETURNCODES = frozenset({124, 143})

# gptme log lines: "[12:00:00] INFO    starting" and "[INFO] starting".
_TIMESTAMPED_LEVEL = re.compile(
    r"^\[(?:\d{1,2}:)?\d{2}:\d{2}:\d{2}\]\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b"
)
_BRACKET_LEVEL = re.compile(r"^\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\](?:\s|$)")
_BARE_LEVEL = re.compile(r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s{2,}")

_IGNORABLE_EXACT = {
    "Warning: Input is not a terminal (fd=0).",
}
_IGNORABLE_PREFIXES = (
    "WARNING  Failed to load plugin ",
    "WARNING  OpenTelemetry dependencies not available",
)


def is_meaningful_progress(line: str) -> bool:
    """True when a stdout line is useful child progress, not startup log noise."""
    stripped = line.strip()
    if not stripped:
        return False
    if stripped in _IGNORABLE_EXACT:
        return False
    if any(stripped.startswith(prefix) for prefix in _IGNORABLE_PREFIXES):
        return False
    for pattern in (_TIMESTAMPED_LEVEL, _BRACKET_LEVEL, _BARE_LEVEL):
        match = pattern.match(stripped)
        if match:
            return match.group(1) in {"ERROR", "CRITICAL"}
    return True


def append_progress(buffer: deque[str], line: str) -> None:
    """Append a truncated progress line onto a bounded deque."""
    trimmed = line.strip()
    if not trimmed:
        return
    buffer.append(trimmed[:MAX_PROGRESS_LINE_CHARS])


def bound_progress(
    lines: Sequence[str], *, max_chars: int = MAX_PROGRESS_CHARS
) -> list[str]:
    """Keep the newest lines that fit in max_chars."""
    kept: list[str] = []
    total = 0
    for line in reversed(list(lines)):
        extra = len(line) + (1 if kept else 0)
        if kept and total + extra > max_chars:
            break
        if not kept and len(line) > max_chars:
            return [line[-max_chars:]]
        kept.append(line)
        total += extra
    kept.reverse()
    return kept


def classify_lifecycle(
    *,
    first_meaningful_at: float | None,
    last_meaningful_at: float | None,
    now: float,
    silence_seconds: float = SILENCE_SECONDS,
) -> str:
    """Evidence-based lifecycle: startup, working, or silent.

    First process stdout is not a meaningful answer. Without a meaningful
    line the child is still in startup, even if logs already arrived.
    """
    if first_meaningful_at is None:
        return "startup"
    quiet_from = (
        last_meaningful_at if last_meaningful_at is not None else first_meaningful_at
    )
    if now - quiet_from > silence_seconds:
        return "silent"
    return "working"


def completion_status_from_returncode(
    returncode: object, *, cancelled: bool = False
) -> str:
    if cancelled:
        return "cancelled"
    if returncode == 0:
        return "success"
    if isinstance(returncode, int) and returncode in _TIMEOUT_RETURNCODES:
        return "timed_out"
    return "error"


def seed_completions_from_timings(
    timings: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Turn archived per-call timings into recent_completions for a resumed leg.

    Task ids are prefixed with ``prior:`` so they cannot collide with the new
    call's ``task-1``. Output text is not in the timing record; do not invent it.
    """
    seeded: list[dict[str, Any]] = []
    for record in timings:
        task_id = str(record.get("task_id") or "task")
        preview = str(record.get("task_preview") or "")
        inner = record.get("timings")
        elapsed = None
        if isinstance(inner, Mapping):
            elapsed = inner.get("total_seconds")
        seeded.append(
            {
                "task_id": f"prior:{task_id}",
                "task": preview[:120],
                "status": completion_status_from_returncode(
                    record.get("returncode"),
                    cancelled=bool(record.get("cancelled")),
                ),
                "returncode": (
                    record.get("returncode")
                    if isinstance(record.get("returncode"), int)
                    else None
                ),
                "elapsed_seconds": elapsed,
                "output_preview": None,
                "source": "prior_leg",
            }
        )
    return seeded
