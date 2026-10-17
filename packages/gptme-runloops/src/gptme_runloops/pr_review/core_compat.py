"""Conversion from gptme-runloops ReviewArtifact to the canonical core format.

The canonical format is ``gptme.util.review.ReviewArtifact`` (``schema_version: 2``).
The dict produced by :func:`to_core_dict` is directly readable by
``gptme.util.review.ReviewArtifact.from_dict`` without loss of gate-relevant
fields — no translation layer is needed on the consumer side.

This module carries *no gptme dependency*: it operates on
:class:`~gptme_runloops.pr_review.schema.ReviewArtifact` instances and produces
plain Python dicts, so gptme does not need to be installed in the same env.

Usage::

    from gptme_runloops.pr_review.core_compat import to_core_dict
    artifact = ...  # ReviewArtifact from pr_review
    d = to_core_dict(artifact)
    # d is schema_version: 2, ready for gptme.util.review.ReviewArtifact.from_dict
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gptme_runloops.pr_review.schema import ReviewArtifact, ReviewFinding

# ---------------------------------------------------------------------------
# Severity mapping: runloops 5-level → core 4-level
# ---------------------------------------------------------------------------
# Runloops uses Pydantic Severity enum: critical / high / medium / low / info
# Core FindingSeverity: critical / error / warning / note
_SEVERITY_MAP: dict[str, str] = {
    "critical": "critical",
    "high": "error",
    "medium": "warning",
    "low": "note",
    "info": "note",
}

# ---------------------------------------------------------------------------
# Disposition mapping: runloops → core FindingStatus
# ---------------------------------------------------------------------------
# The two vocabularies disagree on "confirmed": in runloops it means the
# reviewer validated the finding as a real defect; in core "confirmed" means
# the *fix* was confirmed correct. Mapping runloops "confirmed" → core
# "confirmed" would exclude live bugs from open_findings, making review-watch
# report "nothing to fix". Both actionable dispositions collapse to "open".
_DISPOSITION_MAP: dict[str, str] = {
    "confirmed": "open",
    "needs_validation": "open",
    "dropped": "dropped",
}

# ---------------------------------------------------------------------------
# MergeSafety mapping: runloops → core (values are identical, lowercase)
# ---------------------------------------------------------------------------
_MERGE_SAFETY_MAP: dict[str, str] = {
    "safe": "safe",
    "unsafe": "unsafe",
    "needs_review": "needs_review",
    "unknown": "unknown",
}


def _parse_line_range(raw: object) -> int | None:
    """Extract the first line number from a ``'42'`` or ``'42-49'`` range string."""
    if not isinstance(raw, str):
        return None
    head = raw.split("-", 1)[0].strip()
    try:
        return int(head)
    except ValueError:
        return None


def finding_to_core_dict(finding: ReviewFinding) -> dict:
    """Convert one runloops :class:`ReviewFinding` to a core-compatible dict.

    Gate-relevant fields preserved:
    - ``id`` → ``independence_key`` (union field, finding fingerprint)
    - ``category`` → ``category``
    - ``severity`` (5-level) → ``severity`` (4-level, via :data:`_SEVERITY_MAP`)
    - ``disposition`` → ``status`` (via :data:`_DISPOSITION_MAP`)
    - ``file_path`` → ``file``
    - ``line_range`` → ``line`` (first integer from the range string)
    - ``title`` / ``description`` / ``evidence`` → ``body`` (joined)

    Non-gate fields dropped: ``confidence``, ``validated_by``,
    ``validation_note``, ``published``, ``line_side``, ``fix_hint``.
    """
    severity_raw = (
        finding.severity.value
        if hasattr(finding.severity, "value")
        else str(finding.severity)
    )
    disposition_raw = (
        finding.disposition.value
        if hasattr(finding.disposition, "value")
        else str(finding.disposition)
    )

    parts = [
        v.strip()
        for v in [finding.title, finding.description, finding.evidence]
        if v and v.strip()
    ]
    body = "\n\n".join(parts)

    d: dict = {
        "body": body,
        "file": finding.file_path,
        "line": _parse_line_range(finding.line_range),
        "severity": _SEVERITY_MAP.get(severity_raw, "warning"),
        "status": _DISPOSITION_MAP.get(disposition_raw, "open"),
    }
    if finding.id:
        d["independence_key"] = finding.id
    if finding.category:
        d["category"] = finding.category
    return d


def to_core_dict(artifact: ReviewArtifact) -> dict:
    """Convert a runloops :class:`ReviewArtifact` to a core-compatible dict.

    The returned dict matches ``gptme.util.review.ReviewArtifact`` with
    ``schema_version: 2`` and is directly readable by
    ``ReviewArtifact.from_dict`` with no further translation. Conversion errors
    propagate to the caller rather than returning a partial artifact with missing
    findings.

    Gate-relevant artifact fields preserved:
    - ``target`` (hosted) → ``pr: {owner, repo, number}`` + ``target_kind: "hosted"``
    - ``target`` (local) → ``pr: {owner, repo, number: 0}`` (repo_name split)
      + ``target_kind: "local"`` + ``diff_fingerprint`` from the local target
    - ``merge_safety`` → ``merge_safety`` (same vocabulary)
    - ``findings`` → converted via :func:`finding_to_core_dict`
    """
    from gptme_runloops.pr_review.schema import LocalReviewTarget, ReviewTarget

    target = artifact.target
    merge_safety_raw = (
        artifact.merge_safety.value
        if hasattr(artifact.merge_safety, "value")
        else str(artifact.merge_safety)
    )

    if isinstance(target, ReviewTarget):
        repo_parts = target.repo.split("/", 1)
        pr_owner = repo_parts[0] if len(repo_parts) >= 1 else ""
        pr_repo = repo_parts[1] if len(repo_parts) >= 2 else ""
        pr_number = target.pr_number
        target_kind = "hosted"
        diff_fingerprint = ""
    elif isinstance(target, LocalReviewTarget):
        if "/" in target.repo_name:
            pr_owner, pr_repo = target.repo_name.split("/", 1)
        else:
            pr_owner = ""
            pr_repo = target.repo_name
        pr_number = 0
        target_kind = "local"
        diff_fingerprint = target.diff_fingerprint
    else:
        # Unknown target type — best-effort extraction
        pr_owner = ""
        pr_repo = getattr(target, "repo_name", "")
        pr_number = getattr(target, "pr_number", 0)
        target_kind = getattr(target, "kind", "unknown")
        diff_fingerprint = getattr(target, "diff_fingerprint", "")

    findings_dicts = [finding_to_core_dict(f) for f in artifact.findings]

    d: dict = {
        "schema_version": 2,
        "pr": {
            "owner": pr_owner,
            "repo": pr_repo,
            "number": pr_number,
        },
        "review_status": "complete",
        "merge_safety": _MERGE_SAFETY_MAP.get(merge_safety_raw, "unknown"),
        "validation_errors": 0,
        "findings": findings_dicts,
    }
    if target_kind:
        d["target_kind"] = target_kind
    if diff_fingerprint:
        d["diff_fingerprint"] = diff_fingerprint
    return d
