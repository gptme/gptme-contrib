"""Ingest lesson-injection events emitted by the Claude Code hook.

The ``match-lessons.py`` Claude Code hook appends one JSON object per injected
lesson to ``$TMPDIR/cc-session-{id}-lessons.jsonl`` (see
``scripts/claude-code-hooks/match-lessons.py::_get_lesson_events_file``).  Until
this module existed the producer ran on every session while nothing ever read
the files back, so ``lesson_events`` never reached a session record.

The filename contract is duplicated from the hook on purpose: the hook must stay
dependency-free, so it cannot import from this package.  ``test_lesson_events``
pins the two implementations together.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Upper bound on events attached to one record. Long sessions inject a few
#: hundred lessons; the cap stops a runaway hook from bloating the ledger.
MAX_LESSON_EVENTS = 500


def lesson_events_path(harness_session_id: str) -> Path:
    """Return the hook's events file for ``harness_session_id``.

    Mirrors ``match-lessons.py::_get_lesson_events_file``.
    """
    tmpdir = Path(os.environ.get("TMPDIR", "/tmp"))
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", harness_session_id)
    return tmpdir / f"cc-session-{safe_id}-lessons.jsonl"


def load_lesson_events(
    harness_session_id: str | None = None,
    *,
    max_events: int = MAX_LESSON_EVENTS,
) -> list[dict[str, Any]]:
    """Load lesson-injection events for a harness session.

    Parameters
    ----------
    harness_session_id:
        The harness-native session id the hook used to name its file — for
        Claude Code, the transcript UUID.  Defaults to ``$CC_SESSION_ID``, which
        the autonomous runner exports before launching the harness, so callers
        running inside the session's process tree need not pass anything.
    max_events:
        Truncate to this many events, keeping the earliest.

    Returns an empty list when the id is unknown or the file is absent; a
    missing file is the normal case for non-Claude-Code harnesses.
    """
    if harness_session_id is None:
        harness_session_id = os.environ.get("CC_SESSION_ID") or ""
    if not harness_session_id:
        return []

    path = lesson_events_path(harness_session_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # The hook appends while the session runs, so the trailing line can
            # be mid-write. Skip it rather than dropping the whole file.
            continue
        if isinstance(event, dict):
            events.append(event)

    if len(events) > max_events:
        logger.warning(
            "truncating lesson_events for %s: %d events > max %d",
            harness_session_id,
            len(events),
            max_events,
        )
        events = events[:max_events]
    return events
