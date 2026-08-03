"""Versioned schema for PR review artifacts.

Designed to be forge-neutral: GitHub and Forgejo adapters can produce the same
ReviewArtifact; the renderer decides how to publish it.

Version history:
  v1 (2026-08-03): initial schema — target identity, run metadata, findings,
                   fingerprints, merge safety verdict.
  v1 (2026-08-03): added LocalReviewTarget for working-tree and commit-range
                   reviews that require no forge access (Phase 1).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Discriminator, Field, Tag

SCHEMA_VERSION = "v1"


class Severity(str, Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    info = "info"


class Disposition(str, Enum):
    confirmed = "confirmed"
    needs_validation = "needs_validation"
    dropped = "dropped"


class MergeSafety(str, Enum):
    safe = "safe"
    unsafe = "unsafe"
    needs_review = "needs_review"
    unknown = "unknown"


class ReviewTarget(BaseModel):
    """Immutable identity of a hosted PR being reviewed.

    Binding reviews to exact base/head SHAs prevents stale-head publication.
    """

    kind: Literal["hosted"] = "hosted"
    forge: Literal["github", "forgejo"] = "github"
    repo: str  # e.g. "gptme/gptme-contrib"
    pr_number: int
    base_sha: str
    head_sha: str


class LocalReviewTarget(BaseModel):
    """Target for a local-only review — no forge access required.

    Used for working-tree reviews (in-session, before a PR exists) and for
    reviewing an explicit committed commit range locally.

    Committed/range reviews bind to immutable base/head SHAs.
    Working-tree reviews record the base SHA plus a deterministic diff fingerprint
    (since pretending an uncommitted head is immutable would be wrong).
    """

    kind: Literal["local"] = "local"
    checkout: str  # Absolute path to the repository root
    repo_name: str = ""  # e.g. "gptme/gptme-contrib" — for display; may be empty
    base_sha: str  # Always resolved; working-tree reviews use HEAD
    head_sha: str | None = None  # None for working-tree reviews
    diff_fingerprint: str = ""  # SHA256[:16] of diff bytes (working-tree only)

    @property
    def description(self) -> str:
        name = self.repo_name or self.checkout
        if self.head_sha:
            return f"{name} {self.base_sha[:8]}..{self.head_sha[:8]}"
        return f"{name} working-tree@{self.base_sha[:8]} (fingerprint:{self.diff_fingerprint})"


def _discriminate_target(v: object) -> str:
    """Return the discriminator tag for Union[ReviewTarget, LocalReviewTarget].

    Existing artifacts without an explicit 'kind' default to 'hosted' for
    backward compatibility with Phase 0 artifacts.
    """
    if isinstance(v, dict):
        return str(v.get("kind", "hosted"))
    return str(getattr(v, "kind", "hosted"))


AnyReviewTarget = Annotated[
    Annotated[ReviewTarget, Tag("hosted")] | Annotated[LocalReviewTarget, Tag("local")],
    Discriminator(_discriminate_target),
]


class ReviewFinding(BaseModel):
    """A single finding from the review agent.

    Compatible with the multi-lens-review SKILL.md schema; extends it with
    disposition tracking, fingerprinting, and publication state.
    """

    # Identity
    id: str = Field(
        description="Stable fingerprint (see fingerprint / local_fingerprint)"
    )

    # Classification
    category: str = Field(description="e.g. correctness, security, test-coverage")
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)

    # Location — required; no evidence without a file reference
    file_path: str
    line_range: str = Field(description="e.g. '42' or '42-49'")

    # Content
    title: str
    description: str
    evidence: str = Field(description="Concrete code evidence tied to the location")
    fix_hint: str = ""

    # Lifecycle
    disposition: Disposition = Disposition.needs_validation
    published: bool = False

    # Validation pass (for high-severity findings before publishing)
    validated_by: str | None = None
    validation_note: str = ""

    @classmethod
    def fingerprint(
        cls,
        *,
        repo: str,
        pr_number: int,
        head_sha: str,
        file_path: str,
        title: str,
        prompt_version: str,
    ) -> str:
        """Stable fingerprint for a hosted-PR finding.

        Same finding on the same head SHA + prompt version always produces the
        same fingerprint, so re-runs cannot post duplicate comments.
        """
        payload = json.dumps(
            {
                "repo": repo,
                "pr": pr_number,
                "head": head_sha,
                "file": file_path,
                "title": title,
                "prompt": prompt_version,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @classmethod
    def local_fingerprint(
        cls,
        *,
        repo: str,
        head_sha: str,
        file_path: str,
        title: str,
        prompt_version: str,
    ) -> str:
        """Stable fingerprint for a local-review finding (no PR number).

        Uses the diff fingerprint (for working-tree) or the head SHA (for
        range reviews) as the immutable identity anchor.
        """
        payload = json.dumps(
            {
                "repo": repo,
                "head": head_sha,
                "file": file_path,
                "title": title,
                "prompt": prompt_version,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


class FindingFingerprint(BaseModel):
    """Cross-run fingerprint record for idempotency checking."""

    finding_id: str
    repo: str
    pr_number: int
    head_sha: str
    prompt_version: str
    published_at: datetime | None = None
    comment_id: str | None = None


class ReviewArtifact(BaseModel):
    """Complete, versioned output of one PR review run.

    The analysis core produces this; the adapter (GitHub / Forgejo publisher)
    reads it. The core must not call forge APIs directly.

    Supports both hosted-PR targets (ReviewTarget) and local-only targets
    (LocalReviewTarget) via a discriminated union.
    """

    schema_version: str = SCHEMA_VERSION

    # Immutable identity — hosted PR or local working-tree/range
    target: AnyReviewTarget

    # Run provenance
    model: str
    prompt_version: str
    started_at: datetime
    completed_at: datetime

    # Summary
    summary: str
    merge_safety: MergeSafety

    # Findings
    findings: list[ReviewFinding] = Field(default_factory=list)

    # Publication state
    dry_run: bool = True
    published_at: datetime | None = None

    @property
    def confirmed_findings(self) -> list[ReviewFinding]:
        return [f for f in self.findings if f.disposition == Disposition.confirmed]

    @property
    def token_id(self) -> str:
        """Short readable identity for log messages."""
        t = self.target
        if isinstance(t, ReviewTarget):
            return f"{t.repo}#{t.pr_number}@{t.head_sha[:8]}"
        # LocalReviewTarget
        name = t.repo_name or t.checkout.split("/")[-1]
        head = (t.head_sha or t.diff_fingerprint or "")[:8]
        return f"{name}@{head}"
