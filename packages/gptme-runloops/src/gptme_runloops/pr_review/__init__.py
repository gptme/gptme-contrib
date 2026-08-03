"""PR review core — schema, corpus, and review runner.

Phase 0: versioned finding schema + golden corpus for model evaluation.
Phase 1: local CLI runner — working-tree / commit-range reviews without forge.
Phase 2 (later): GitHub publisher with idempotency guards + PM integration.
"""

from .reviewer import REVIEW_PROMPT_VERSION, resolve_local_target, run_review
from .schema import (
    AnyReviewTarget,
    Disposition,
    FindingFingerprint,
    LocalReviewTarget,
    MergeSafety,
    ReviewArtifact,
    ReviewFinding,
    ReviewTarget,
    Severity,
)

__all__ = [
    # Schema
    "AnyReviewTarget",
    "Disposition",
    "FindingFingerprint",
    "LocalReviewTarget",
    "MergeSafety",
    "ReviewArtifact",
    "ReviewFinding",
    "ReviewTarget",
    "Severity",
    # Phase 1 runner
    "REVIEW_PROMPT_VERSION",
    "resolve_local_target",
    "run_review",
]
