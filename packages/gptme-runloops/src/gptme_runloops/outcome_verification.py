"""Cheap post-dispatch outcome verification.

Records whether a PR/issue changed (new commit/comment/thread resolved/state change)
since a dispatch started. Uses one bounded gh call for cost efficiency.
"""

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class OutcomeVerification:
    """Result of an outcome check."""

    repo: str
    number: int
    changed: bool  # True if item state/content changed since started_at
    reason: str  # Why it changed (new_commit, new_comment, thread_resolved, state_changed, no_change)
    started_at: datetime
    checked_at: datetime

    def to_dict(self) -> dict:
        """Serialize to JSON-compatible dict."""
        return {
            "repo": self.repo,
            "number": self.number,
            "changed": self.changed,
            "reason": self.reason,
            "started_at": self.started_at.isoformat(),
            "checked_at": self.checked_at.isoformat(),
        }


def check_outcome_changed(
    repo: str,
    number: int,
    started_at: datetime,
) -> OutcomeVerification:
    """Check if a PR/issue changed since dispatch started.

    Performs one bounded 'gh pr view --json' call to detect:
    - New commit (count changed or updatedAt > started_at)
    - New comment (comments changed)
    - Thread resolved (review threads changed state)
    - State changed (merged/closed/open transitions)

    Args:
        repo: Repo slug (owner/repo)
        number: PR/issue number
        started_at: When the dispatch started (used to detect new changes)

    Returns:
        OutcomeVerification with changed=True if any change detected.
    """
    checked_at = datetime.now(timezone.utc)

    try:
        # Single bounded gh call: get PR metadata
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                "--repo",
                repo,
                str(number),
                "--json",
                "updatedAt,state,commits,comments,reviews,title",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if proc.returncode != 0:
            # gh command failed — conservative: assume changed (don't silence the item)
            return OutcomeVerification(
                repo=repo,
                number=number,
                changed=True,
                reason="gh_call_failed",
                started_at=started_at,
                checked_at=checked_at,
            )

        data = json.loads(proc.stdout)

        # Parse updatedAt timestamp
        updated_str = data.get("updatedAt", "")
        if not updated_str:
            # No updatedAt — treat as no change
            return OutcomeVerification(
                repo=repo,
                number=number,
                changed=False,
                reason="no_change",
                started_at=started_at,
                checked_at=checked_at,
            )

        # Parse the ISO timestamp (e.g. "2026-08-10T16:02:30Z")
        try:
            updated_at = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            # Parsing failed — conservative: assume changed
            return OutcomeVerification(
                repo=repo,
                number=number,
                changed=True,
                reason="parse_error",
                started_at=started_at,
                checked_at=checked_at,
            )

        # Detect changes: if updatedAt > started_at, something changed
        if updated_at > started_at:
            # Item was updated after dispatch started — changed
            # (Could be commit, comment, review, state change, title, etc)
            return OutcomeVerification(
                repo=repo,
                number=number,
                changed=True,
                reason="updated_after_start",
                started_at=started_at,
                checked_at=checked_at,
            )

        # No detectable change
        return OutcomeVerification(
            repo=repo,
            number=number,
            changed=False,
            reason="no_change",
            started_at=started_at,
            checked_at=checked_at,
        )

    except subprocess.TimeoutExpired:
        # gh call timed out — conservative: assume changed
        return OutcomeVerification(
            repo=repo,
            number=number,
            changed=True,
            reason="timeout",
            started_at=started_at,
            checked_at=checked_at,
        )
    except Exception as exc:
        # Any other error — conservative: assume changed (don't silence)
        return OutcomeVerification(
            repo=repo,
            number=number,
            changed=True,
            reason=f"error:{type(exc).__name__}",
            started_at=started_at,
            checked_at=checked_at,
        )


def append_outcome_verification_ledger(
    ledger_path: str | Path,
    verification: OutcomeVerification,
) -> None:
    """Append an outcome verification record to the ledger (JSONL)."""
    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(verification.to_dict(), ensure_ascii=False) + "\n")
