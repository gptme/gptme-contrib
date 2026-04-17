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

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .record import SessionRecord
from .signals import extract_from_path
from .store import SessionStore

logger = logging.getLogger(__name__)

#: Default path for grading weights config (Phase 3 multivariate grading).
_GRADING_WEIGHTS_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent.parent.parent
    / "state"
    / "grading-weights.json"
)

#: Fallback weights when config file is not found.
_DEFAULT_WEIGHTS: dict[str, float] = {
    "productivity": 0.40,
    "alignment": 0.35,
    "harm": 0.25,
}


def load_grading_weights() -> dict[str, float]:
    """Load grading weights from state/grading-weights.json, with fallback."""
    # Try to find grading-weights.json by walking up from this file
    path = _GRADING_WEIGHTS_PATH
    if path.exists():
        try:
            data: dict[str, float] = json.loads(path.read_text())
            return data
        except Exception as e:
            logger.warning("Failed to load grading weights from %s: %s", path, e)
    # Try environment variable for agent workspace
    agent_path = os.environ.get("AGENT_PATH") or os.environ.get("GPTME_AGENT_PATH")
    if agent_path:
        alt = Path(agent_path) / "state" / "grading-weights.json"
        if alt.exists():
            try:
                alt_data: dict[str, float] = json.loads(alt.read_text())
                return alt_data
            except Exception as e:
                logger.warning("Failed to load grading weights from %s: %s", alt, e)
    return _DEFAULT_WEIGHTS


#: Valid values for the ``context_tier`` parameter.  Exported so ``cli.py``
#: can use a single source of truth for ``click.Choice``.
VALID_CONTEXT_TIERS: frozenset[str] = frozenset({"standard", "extended", "large", "massive"})

#: Valid values for the ``ab_group`` parameter.  Exported so ``cli.py``
#: can use a single source of truth for ``click.Choice``.
VALID_AB_GROUPS: frozenset[str] = frozenset({"treatment", "control"})


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
    context_tier: str | None = None,
    ab_group: str | None = None,
    tier_version: str | None = None,
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
    context_tier:
        Context tier used for this session (e.g. ``"standard"``, ``"massive"``).
        Enables A/B comparison of context inclusion strategies.
    ab_group:
        A/B group assignment for this session (e.g. ``"treatment"`` or ``"control"``).
    tier_version:
        Version of the context tier configuration used for this session.
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
        When ``None`` and a trajectory is available, auto-detected from
        the first ``/journal/`` write in the trajectory signals.
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
    4. ``exit_code == 124`` (timeout, no other evidence) → ``"unknown"``
    5. Default: ``"unknown"`` (no signal available — callers should not
       treat this as productive *or* penalize it in bandits)
    6. Override: if step 2–5 yielded ``"noop"`` or ``"unknown"`` but
       ``deliverables`` is
       non-empty, upgrade to ``"productive"`` (trajectory may miss commits
       detected by the caller via ``git diff``).
    """
    if context_tier is not None and context_tier not in VALID_CONTEXT_TIERS:
        raise ValueError(
            f"Invalid context_tier {context_tier!r}. Expected one of {sorted(VALID_CONTEXT_TIERS)}"
        )
    if ab_group is not None and ab_group not in VALID_AB_GROUPS:
        raise ValueError(
            f"Invalid ab_group {ab_group!r}. Expected one of {sorted(VALID_AB_GROUPS)}"
        )

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

        # Use session_duration_s from signals when caller didn't provide duration.
        # Needed for Claude Code sessions where the Stop hook doesn't track wall-clock time.
        if duration_seconds == 0 and signals:
            duration_seconds = int(signals.get("session_duration_s") or 0)

        # Use model from trajectory signals when caller didn't provide one.
        # Needed when the hook payload doesn't include the model name (e.g. CC Stop hook).
        if (not model or model == "unknown") and signals:
            traj_model = (signals.get("usage") or {}).get("model")
            if traj_model:
                model = traj_model

    # --- Resolve deliverables ---
    # Merge shell-provided deliverables (bare SHAs) with trajectory-derived
    # ones (commit messages, file write paths).  The shell always passes a
    # list (possibly empty), so we treat empty the same as None.
    # Capture caller-supplied deliverables *before* the merge so the noop
    # override below can check only caller-provided items (not traj-derived
    # ones that is_productive() may have deliberately excluded).
    caller_deliverables: list[str] = list(deliverables) if deliverables else []
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
    # 4. Default → unknown (no signal — don't feed into bandits)
    #
    # Rationale: defaulting to "productive" inflates bandit reward signals
    # when callers omit trajectory/commit data.  "unknown" means "we don't
    # know" — callers should skip bandit updates rather than guess.
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
        outcome = "unknown"
    else:
        outcome = "unknown"

    # Override noop/unknown → productive if *caller-supplied* deliverables exist.
    # Trajectory signals may miss commits detected by the caller via git diff.
    # Use caller_deliverables (pre-merge) so trajectory-derived items (e.g. a
    # single file write that is_productive() deliberately classifies as noop)
    # do not trigger this override.
    if outcome in ("noop", "unknown") and caller_deliverables:
        logger.info(
            "Overriding outcome %s→productive: %d caller-supplied deliverable(s)",
            outcome,
            len(caller_deliverables),
        )
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
        "exit_code": exit_code,
        "duration_seconds": duration_seconds,
        "deliverables": deliverables,
    }
    if context_tier is not None:
        record_kwargs["context_tier"] = context_tier
    if ab_group is not None:
        record_kwargs["ab_group"] = ab_group
    if tier_version is not None:
        record_kwargs["tier_version"] = tier_version
    if trigger is not None:
        record_kwargs["trigger"] = trigger
    if actual_category is not None:
        record_kwargs["category"] = actual_category
    if recommended_category is not None:
        record_kwargs["recommended_category"] = recommended_category
    if trajectory_path is not None:
        record_kwargs["trajectory_path"] = str(trajectory_path)
    # Fallback: if caller didn't provide journal_path, use the first
    # journal path extracted from the trajectory signals.
    if journal_path is None and signals:
        traj_journals = signals.get("journal_paths", [])
        if traj_journals:
            # First chronological write is the journal creation (not a later edit)
            journal_path = traj_journals[0]
            logger.info("Auto-detected journal_path from trajectory: %s", journal_path)
    if journal_path is not None:
        record_kwargs["journal_path"] = journal_path
    if session_id is not None:
        record_kwargs["session_id"] = session_id
    if token_count is not None:
        record_kwargs["token_count"] = token_count
    record = SessionRecord(**record_kwargs)
    if grade is not None:
        record.set_productivity_grade(grade)
        # NOTE: Weighted multi-dim combine (productivity × alignment × harm)
        # is handled by compute-harm-signal.py after harm grades are computed.
        # At post_session time only productivity is available, so there is
        # nothing to combine yet.
    if journal_path is not None:
        try:
            existing_session_ids = [
                r.session_id
                for r in store.load_all()
                if r.journal_path == journal_path and r.session_id != record.session_id
            ]
            if existing_session_ids:
                truncated = existing_session_ids[:8]
                suffix = (
                    f" (and {len(existing_session_ids) - 8} more)"
                    if len(existing_session_ids) > 8
                    else ""
                )
                logger.warning(
                    "journal_path %s already used by other session_ids: %s%s",
                    journal_path,
                    ", ".join(str(sid) for sid in truncated),
                    suffix,
                )
        except Exception as e:
            logger.warning("Duplicate journal_path check failed (non-fatal): %s", e)
    store.append(record)

    return PostSessionResult(
        record=record,
        grade=grade,
        signals=signals,
        token_count=token_count,
    )
