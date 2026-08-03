"""PR review core — schema, corpus, and evaluation tooling.

Phase 0: versioned finding schema + golden corpus for model evaluation.
Phase 1 (next): CLI runner that produces ReviewArtifact JSON without publishing.
Phase 2 (later): GitHub publisher with idempotency guards + PM integration.
"""

from .schema import (
    Disposition,
    FindingFingerprint,
    MergeSafety,
    ReviewArtifact,
    ReviewFinding,
    ReviewTarget,
    Severity,
)

__all__ = [
    "Disposition",
    "FindingFingerprint",
    "MergeSafety",
    "ReviewArtifact",
    "ReviewFinding",
    "ReviewTarget",
    "Severity",
]
