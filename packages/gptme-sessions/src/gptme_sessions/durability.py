"""Durability scoring — measures whether session artifacts survived reversion.

Part of the multivariate session grading rollout (ErikBjare/bob#632).
Adds an ``impact_durability`` dimension: 30+ days after a session,
were any of its shipped commits reverted?

Tier reference (from the adversarial eval analysis at
``knowledge/analysis/adversarial-session-eval-2026-05.md``):

| Durability   | Examples                                                   |
|-------------|------------------------------------------------------------|
| 1.0 — Durable | Bugfix still live, tool still in active use               |
| 0.5 — Ephemeral | Blog post with 0 recorded engagement                    |
| 0.3 — Analysis debt | Research done, no task created from findings           |
| 0.0 — Gone   | Tweet deleted, social artifact gone                        |

This module implements a **commit-reversion signal** only — it answers
"did the shipped code survive?" The richer signals (blog engagement,
tool liveness, tweet deletion) are separate collectors that feed the
same dimension.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .record import SessionRecord

logger = logging.getLogger(__name__)

# ── Public API ──────────────────────────────────────────────────────────────


def compute_durability(
    record: "SessionRecord",
    repo_root: str | Path,
    *,
    age_days: int = 30,
) -> float | None:
    """Compute durability score for a session record.

    Only scores sessions older than *age_days* (default 30) whose
    deliverables include at least one commit SHA.  Returns ``None``
    when the session is too young or has no commit deliverables.

    Returns a ``float`` in [0.0, 1.0]:
    - 1.0: every shipped commit survived without reversion
    - 0.0: every shipped commit was reverted
    - intermediate: some commits survived, some didn't
    """
    if not _is_old_enough(record, age_days):
        return None

    commits = _extract_commit_shas(record)
    if not commits:
        return None

    survived = 0
    for sha in commits:
        if not _commit_was_reverted(sha, repo_root):
            survived += 1

    return survived / len(commits)


def compute_durability_for_store(
    records: list["SessionRecord"],
    repo_root: str | Path,
    *,
    age_days: int = 30,
) -> int:
    """Apply durability scores to a list of SessionRecords in-place.

    For each record that is old enough and has commit deliverables,
    adds a ``durability`` entry to ``record.grades`` and sets
    ``record.grade_reasons["durability"]``.

    Returns the number of records updated.
    """
    updated = 0
    for record in records:
        score = compute_durability(record, repo_root, age_days=age_days)
        if score is None:
            continue
        record.grades["durability"] = score
        record.grade_reasons["durability"] = _build_reason(record, score)
        updated += 1
    return updated


# ── Internal helpers ────────────────────────────────────────────────────────


def _is_old_enough(record: "SessionRecord", age_days: int) -> bool:
    """Return True when the session is old enough for durability scoring."""
    if record.start_time is None and not _guess_timestamp(record):
        return False

    ts = _guess_timestamp(record)
    if ts is None:
        return False

    age = datetime.now(timezone.utc) - ts
    return age.days >= age_days


def _guess_timestamp(record: "SessionRecord") -> datetime | None:
    """Extract the best available timestamp from a session record."""
    if record.start_time is not None:
        try:
            return datetime.fromisoformat(str(record.start_time))
        except (ValueError, TypeError):
            pass
    if record.end_time is not None:
        try:
            return datetime.fromisoformat(str(record.end_time))
        except (ValueError, TypeError):
            pass
    # Fallback: parse timestamp from the session_id if it looks date-like
    ts = getattr(record, "timestamp", None)
    if ts is not None:
        try:
            return datetime.fromisoformat(str(ts))
        except (ValueError, TypeError):
            pass
    return None


def _extract_commit_shas(record: "SessionRecord") -> list[str]:
    """Extract git commit SHAs from a session record's deliverables.

    Handles the deliverable formats seen in practice:
    - bare SHAs (7-40 hex chars): ``"abc1234def"``
    - commit-with-message: ``"fix: description (abc1234d)"``
    - merge-commit: ``"merge-commit (abc1234d)"``
    """
    from .deliverables import deliverable_kind

    shas: list[str] = []
    for value in record.deliverables or []:
        kind = deliverable_kind(value)
        if kind == "commit":
            sha = _sha_from_deliverable(value)
            if sha:
                shas.append(sha)
        elif kind == "merge_commit":
            sha = _sha_from_deliverable(value)
            if sha:
                shas.append(sha)
        # pull_request deliverables track merge outcomes, not direct commits
    return shas


def _sha_from_deliverable(value: str) -> str | None:
    """Extract a full 40-char SHA from a deliverable string."""
    stripped = value.strip()
    # Bare SHA
    if 7 <= len(stripped) <= 40 and all(c in "0123456789abcdef" for c in stripped.lower()):
        return stripped
    # Parenthesized SHA suffix: "message (abc1234d)"
    if stripped.endswith(")") and "(" in stripped:
        candidate = stripped[stripped.rfind("(") + 1 : -1]
        if 7 <= len(candidate) <= 40 and all(c in "0123456789abcdef" for c in candidate.lower()):
            return candidate
    return None


def _commit_was_reverted(sha: str, repo_root: str | Path) -> bool:
    """Return True when *sha* was reverted in the repository.

    Uses two signals:
    1. A commit message body mentions ``This reverts commit <sha>``
    2. A commit message subject contains ``Revert`` and the short SHA
       appears in the full message body
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "log",
                "--all",
                "--oneline",
                "--grep=Revert",
                "--format=%H %s %b",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("git log for revert check timed out: %s", exc)
        return False

    if result.returncode != 0:
        logger.warning("git log failed: %s", result.stderr.strip())
        return False

    # Check both short (7-char min) and full SHA forms in revert messages
    short_sha = sha[:7].lower()
    text = result.stdout.lower()

    if short_sha in text:
        return True

    # Also check common revert patterns that don't include the short SHA
    # in the message but match on the longer SHA form
    if len(sha) > 7 and sha.lower() in text:
        return True

    return False


def _build_reason(record: "SessionRecord", score: float) -> str:
    """Build a human-readable reason string for the durability score."""
    commits = _extract_commit_shas(record)
    n_commits = len(commits)
    if score >= 1.0:
        return (
            f"All {n_commits} shipped commit(s) survived without reversion "
            f"(30+ days post-session)"
        )
    if score <= 0.0:
        return f"All {n_commits} shipped commit(s) were reverted within 7 days"
    survived = round(score * n_commits)
    reverted = n_commits - survived
    return f"{survived}/{n_commits} commit(s) survived, " f"{reverted} reverted within 7 days"
