"""post_session — post-session recording pipeline for gptme agents.

Replaces the session recording and signal-extraction block in
``autonomous-run.sh`` (~80 lines of bash).  Any agent run loop can call
:func:`post_session` after the agent process exits.

Responsibilities:
- Extract signals + grade from trajectory file (if provided)
- Determine outcome (productive / noop / failed) from signals + exit code
- Build and append :class:`~gptme_sessions.record.SessionRecord`
- Return structured result with grade and raw signals for downstream use
  (e.g. bandit updates, NOOP counters, logging)

What this function does **not** do (kept in caller scripts):
- Trajectory *discovery* (sentinel-file timing, CC project dir scanning) —
  harness-specific, stays in shell or harness adapter
- Bandit updates — depend on agent-specific scripts; callers receive the
  ``grade`` in :class:`PostSessionResult` and can update their own bandits
- Event emission, standup writing, git push — also caller responsibilities
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .record import SessionRecord
from .signals import extract_from_path
from .store import SessionStore

logger = logging.getLogger(__name__)


@dataclass
class PostSessionResult:
    """Return value from :func:`post_session`.

    Attributes:
        record:      The :class:`SessionRecord` that was appended to the store.
        grade:       Graded reward (0.0–1.0) extracted from the trajectory, or
                     ``None`` if no trajectory was available.
        signals:     Raw signal dict from :func:`~gptme_sessions.signals.extract_from_path`,
                     or ``None`` if no trajectory was available.
        token_count: Total token count from the trajectory (CC format only),
                     or ``None`` if not available.
    """

    record: SessionRecord
    grade: float | None = None
    signals: dict[str, Any] | None = None
    token_count: int | None = None


def post_session(
    *,
    store: SessionStore,
    harness: str,
    model: str | None = None,
    run_type: str | None = None,
    trigger: str | None = None,
    category: str | None = None,
    recommended_category: str | None = None,
    exit_code: int = 0,
    duration_seconds: int = 0,
    trajectory_path: Path | None = None,
    start_commit: str | None = None,
    end_commit: str | None = None,
    deliverables: list[str] | None = None,
    journal_path: str | None = None,
    session_id: str | None = None,
) -> PostSessionResult:
    """Record a completed agent session and extract trajectory signals.

    Parameters
    ----------
    store:
        :class:`~gptme_sessions.store.SessionStore` to append the record to.
    harness:
        Runtime that ran the session (e.g. ``"claude-code"``, ``"gptme"``).
    model:
        Model string as reported by the harness (e.g. ``"claude-opus-4-6"``).
    run_type:
        Pipeline / trigger name (e.g. ``"autonomous"``, ``"monitoring"``).
        Kept for backward compatibility; prefer ``trigger`` going forward.
    trigger:
        How the session was started: ``"timer"``, ``"dispatch"``, ``"manual"``,
        ``"spawn"``.  Records trigger mechanism as metadata without implying
        bandit treatment.  Added in PR #351.
    category:
        Work category for the session (e.g. ``"code"``, ``"infrastructure"``).
        When provided, used as-is (e.g. from a post-hoc classifier).
        When ``None`` and a trajectory is available, inferred from commit
        messages and file paths.
    recommended_category:
        Category recommended by the selector before the session ran
        (e.g. Thompson sampling, CASCADE). Stored alongside the actual
        category so drift between recommendation and reality is trackable.
    exit_code:
        Exit code from the agent process.  Non-zero (except 124 = timeout)
        marks the session as ``"failed"``.
    duration_seconds:
        Wall-clock duration.  Pass ``int(time.monotonic() - start_time)`` or
        the shell ``$SECONDS`` variable.
    trajectory_path:
        Path to the trajectory ``.jsonl`` file for this session.  Supports
        both gptme (``conversation.jsonl``) and Claude Code formats.
        Signal extraction is skipped if ``None`` or the file does not exist.
    start_commit:
        Git HEAD SHA *before* the session started.  Used for NOOP detection
        when no trajectory is available.
    end_commit:
        Git HEAD SHA *after* the session completed.
    deliverables:
        Explicit list of deliverables (commit SHAs, PR URLs).  If ``None``
        or empty, deliverables are extracted from the trajectory signals.
        If non-empty, they are *merged* with trajectory-derived deliverables
        (duplicates removed).
    journal_path:
        Path to the journal entry written during the session, if any.
    session_id:
        Override the auto-generated session ID.

    Returns
    -------
    PostSessionResult
        Contains the appended record, grade, signals, and token count.

    Outcome determination (priority order)
    ---------------------------------------
    1. ``exit_code not in (0, 124)`` → ``"failed"``
    2. Trajectory ``is_productive()`` → ``"productive"`` / ``"noop"``
    3. Git HEAD comparison (``start_commit != end_commit``) → productive / noop
    4. ``exit_code == 124`` (timeout, no other evidence) → ``"noop"``
    5. Default: ``"productive"``
    """
    grade: float | None = None
    signals: dict[str, Any] | None = None
    token_count: int | None = None
    traj_productive: bool | None = None

    # --- Extract signals from trajectory ---
    if trajectory_path is not None and trajectory_path.is_file():
        try:
            result = extract_from_path(trajectory_path)
            signals = result
            grade = result.get("grade")
            traj_productive = result.get("productive")
            usage = result.get("usage") or {}
            total = usage.get("total_tokens", 0)
            if total:
                token_count = int(total)
        except Exception as e:
            # Signal extraction is non-fatal; proceed without signals
            logger.warning("Signal extraction from %s failed: %s", trajectory_path, e)

    # --- Resolve deliverables ---
    # Merge shell-provided deliverables (bare SHAs) with trajectory-derived
    # ones (commit messages, file write paths).  The shell always passes a
    # list (possibly empty), so we treat empty the same as None.
    traj_deliverables = signals.get("deliverables", []) if signals else []
    if not deliverables:
        deliverables = traj_deliverables
    elif traj_deliverables:
        # Add trajectory items not already present (e.g. file write paths)
        existing = set(deliverables)
        deliverables = deliverables + [d for d in traj_deliverables if d not in existing]

    # --- Determine outcome ---
    # Priority order (highest → lowest):
    # 1. Non-zero exit (except 124) → failed
    # 2. Trajectory productive flag → productive / noop
    # 3. Git HEAD comparison → productive / noop
    # 4. Timeout (124) with no other evidence → noop
    # 5. Default → productive
    if exit_code not in (0, 124):
        outcome = "failed"
    elif traj_productive is not None:
        outcome = "productive" if traj_productive else "noop"
    elif start_commit is not None and end_commit is not None:
        outcome = "productive" if start_commit != end_commit else "noop"
    elif (start_commit is None) != (end_commit is None):
        logger.warning(
            "Only one of start_commit/end_commit provided (%s=%r, %s=%r); git comparison skipped",
            "start_commit",
            start_commit,
            "end_commit",
            end_commit,
        )
        # Apply remaining priority steps: timeout → noop, else → productive
        outcome = "noop" if exit_code == 124 else "productive"
    elif exit_code == 124:
        # Timeout with no trajectory or git evidence → noop
        outcome = "noop"
    else:
        outcome = "productive"

    # --- Category: inferred (actual) vs recommended (intended) ---
    inferred_category = signals.get("inferred_category") if signals else None
    # Actual category: explicit override > inferred from signals
    actual_category = category or inferred_category

    # --- Build SessionRecord kwargs ---
    record_kwargs: dict[str, Any] = {
        "harness": harness,
        "model": model or "unknown",
        "run_type": run_type or "unknown",
        "outcome": outcome,
        "duration_seconds": duration_seconds,
        "deliverables": deliverables,
    }
    if trigger is not None:
        record_kwargs["trigger"] = trigger
    if actual_category is not None:
        record_kwargs["category"] = actual_category
    if recommended_category is not None:
        record_kwargs["recommended_category"] = recommended_category
    if trajectory_path is not None:
        record_kwargs["trajectory_path"] = str(trajectory_path)
    if journal_path is not None:
        record_kwargs["journal_path"] = journal_path
    if session_id is not None:
        record_kwargs["session_id"] = session_id
    if token_count is not None:
        record_kwargs["token_count"] = token_count

    record = SessionRecord(**record_kwargs)
    store.append(record)

    return PostSessionResult(
        record=record,
        grade=grade,
        signals=signals,
        token_count=token_count,
    )
