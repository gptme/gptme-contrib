"""PR review core — schema, corpus, and review runner.

Phase 0: versioned finding schema + golden corpus for model evaluation.
Phase 1: local CLI runner — working-tree / commit-range reviews without forge.
Phase 2 (later): GitHub publisher with idempotency guards + PM integration.

What this module IS: the forge-neutral entry point for reviewing a *local*
diff (``review --working-tree`` / commit range) — ``reviewer.py`` makes zero
forge calls — plus the versioned finding schema and the package shell intended
to host the production review engine once it is promoted here.

What this module is NOT: the reviewer that runs in production, and not a
GitHub publisher today. Two siblings exist:

- ``scripts/github/ai-review.py`` (ErikBjare/bob, **private**) is the reviewer
  that actually posts PR findings on a 20-minute sweep. Its
  ``<!-- bob-ai-review-finding -->`` marker is a wire protocol read by
  ``scripts/github/self-merge-check.py`` and ``scripts/github/activity-gate.sh``
  in this repo — do not emit a competing marker from here.
- ``gptme.util.review`` / ``gptme-util review pr`` (gptme core, public) is the
  model-facing primitive and the source of truth for the artifact schema.

The one-shot prompt in ``reviewer.py`` should not be developed further; the
promoted engine replaces it. Comparison, retirement plan, and a routing table
("which tool for which task") live in
``knowledge/technical/pr-review-systems-map.md`` in the ErikBjare/bob
workspace.
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
