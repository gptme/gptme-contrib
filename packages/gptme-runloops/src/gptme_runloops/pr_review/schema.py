"""Versioned schema for PR review artifacts.

Designed to be forge-neutral: GitHub and Forgejo adapters can produce the same
ReviewArtifact; the renderer decides how to publish it.

Version history:
  v1 (2026-08-03): initial schema — target identity, run metadata, findings,
                   fingerprints, merge safety verdict.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

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
    """Immutable identity of the PR being reviewed.

    Binding reviews to exact base/head SHAs prevents stale-head publication.
    """

    forge: Literal["github", "forgejo"] = "github"
    repo: str  # e.g. "gptme/gptme-contrib"
    pr_number: int
    base_sha: str
    head_sha: str


class ReviewFinding(BaseModel):
    """A single finding from the review agent.

    Compatible with the multi-lens-review SKILL.md schema; extends it with
    disposition tracking, fingerprinting, and publication state.
    """

    # Identity
    id: str = Field(description="Stable fingerprint (see FindingFingerprint)")

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
        """Stable, content-addressed fingerprint for deduplication.

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
    """

    schema_version: str = SCHEMA_VERSION

    # Immutable identity
    target: ReviewTarget

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
        return f"{t.repo}#{t.pr_number}@{t.head_sha[:8]}"
