"""Validate Claude parent-transcript ownership before consuming its evidence."""

from pathlib import Path
from typing import Any

from .signals import parse_trajectory


class TrajectoryIdentityError(ValueError):
    """The parent transcript does not belong to the expected native session."""

    def __init__(self, path: Path, expected: str, observed: set[str]):
        self.evidence: dict[str, Any] = {
            "reason": "claude_session_identity_mismatch",
            "path": str(path),
            "expected_session_id": expected,
            "observed_session_ids": sorted(observed),
        }
        super().__init__(
            f"Claude transcript identity mismatch: expected {expected}, got {sorted(observed)}"
        )


def validate_claude_trajectory(path: Path, expected_session_id: str | None) -> None:
    """Require one matching parent UUID when a caller supplies an identity.

    Native persistence JSONL uses sessionId; stream-json uses session_id.
    Inspect only the parent file, before subagents (which own different UUIDs)
    are combined. A filename, timestamp or configured model is not ownership.
    Legacy callers without a native UUID retain their existing behaviour.
    """
    if not expected_session_id:
        return
    observed = {
        value
        for row in parse_trajectory(path)
        for key in ("sessionId", "session_id")
        if isinstance(value := row.get(key), str) and value
    }
    if observed != {expected_session_id}:
        raise TrajectoryIdentityError(path, expected_session_id, observed)
