"""Helpers for structured deliverable provenance records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

OwnershipVerdict = Literal[
    "trajectory_observed",
    "session_trailer_owned",
    "explicitly_foreign",
    "ambiguous",
]


def looks_like_sha(value: str) -> bool:
    """Return True for bare SHA-like hex strings used as deliverables."""
    lowered = value.lower().strip()
    return 7 <= len(lowered) <= 40 and all(c in "0123456789abcdef" for c in lowered)


def sha_in_traj_prefixes(sha: str, traj_sha_prefixes: set[str] | None) -> bool:
    """Return True if a caller SHA matches a trajectory commit prefix."""
    if not traj_sha_prefixes:
        return False
    sha_lower = sha.lower().strip()
    return any(sha_lower.startswith(prefix) for prefix in traj_sha_prefixes)


def classify_commit_ownership(
    sha: str,
    *,
    session_id: str | None = None,
    trailer_ids: Sequence[str] | None = None,
    traj_sha_prefixes: set[str] | None = None,
) -> OwnershipVerdict:
    """Return the typed ownership verdict for one candidate commit SHA.

    A bound trajectory is authoritative: a SHA observed there is owned even
    when git trailers are missing. A ``Git-Session-Id`` trailer matching
    ``session_id`` is owned even if the trajectory missed the SHA. Any other
    trailer is explicitly foreign. An untagged shared-range SHA is
    *ambiguous*, not session-owned — keeping it is how session 73be credited
    six sibling commits.
    """
    if sha_in_traj_prefixes(sha, traj_sha_prefixes):
        return "trajectory_observed"
    values = [str(v).strip() for v in (trailer_ids or []) if str(v).strip()]
    if values:
        if session_id and session_id in values:
            return "session_trailer_owned"
        return "explicitly_foreign"
    return "ambiguous"


def deliverable_kind(value: str) -> str:
    """Infer the deliverable kind for file, commit, and PR-style values."""
    stripped = value.strip()
    if stripped.startswith("merge-commit (") and stripped.endswith(")"):
        return "merge_commit"
    if stripped.startswith("merge PR #") or stripped.startswith("PR #") or "/pull/" in stripped:
        return "pull_request"
    if looks_like_sha(stripped):
        return "commit"
    if stripped.endswith(")") and "(" in stripped:
        candidate = stripped[stripped.rfind("(") + 1 : -1]
        if looks_like_sha(candidate):
            return "commit"
    return "file"


def build_deliverable_detail(
    value: str,
    *,
    provenance_class: str,
    evidence: Mapping[str, Any],
    kind: str | None = None,
) -> dict[str, Any]:
    """Build one structured deliverable detail entry."""
    compact_evidence = {
        key: val for key, val in evidence.items() if val not in (None, "", [], {}, ())
    }
    return {
        "value": value,
        "kind": kind or deliverable_kind(value),
        "provenance_class": provenance_class,
        "evidence": compact_evidence,
    }


def project_deliverable_details(
    deliverables: list[str],
    detail_by_value: Mapping[str, dict[str, Any]],
    *,
    fallback_evidence: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return one detail per deliverable in legacy order, gap-filling when needed."""
    return [
        detail_by_value.get(value)
        or build_deliverable_detail(
            value,
            provenance_class="fallback_observed",
            evidence=fallback_evidence,
        )
        for value in deliverables
    ]
