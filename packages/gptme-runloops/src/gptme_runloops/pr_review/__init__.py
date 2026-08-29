"""PR review core — schema, corpus, and local review runner.

Phase 0: versioned finding schema + golden corpus for model evaluation.
Phase 1: local CLI runner — working-tree / commit-range reviews without forge.

What this package is for
------------------------
Reviewing a **local diff with no forge involved**: ``gptme-runloops review
--working-tree`` (or ``--base``/``--head``). ``reviewer.py`` makes zero forge
API calls by construction; ``github_adapter.py`` is the quarantined I/O
boundary that callers use, not a dependency of the review logic. This is the
entry point to reach for when you want findings on uncommitted work.

What this package is NOT
------------------------
It is **not** the reviewer that reviews Bob's PRs. It has never carried
production traffic, and it has no consensus passes, finding suppression, or
soundness gates — the machinery that makes posted findings tolerable to a
human. Do not extend it into a second GitHub-posting reviewer, and do not
treat its review prompt as canonical: per the decision below this package is
the *destination shell*, and the production review engine is being lifted into
it rather than reimplemented here.

Related implementations
-----------------------
Three PR-review implementations exist across three repos:

- **this package** (gptme-contrib) — forge-neutral local-diff reviews.
- **``gptme.util.review`` + ``gptme-util review pr``** (gptme core, public) —
  canonical finding schema and the model-facing diff→findings primitive.
  Produces an artifact, writes nothing.
- **Bob's ``scripts/github/ai-review.py``** (private brain repo) — the only
  implementation with production traffic; posts marker + inline comments to
  GitHub, with consensus filtering and a suppression ledger. Its
  ``<!-- bob-ai-review-finding -->`` marker is a wire protocol read by this
  repo's ``self-merge-check.py`` and ``activity-gate.sh``.

Full comparison, the canonical-implementation decision, migration steps and a
"which tool for which task" routing table live in Bob's brain repo (private)
at ``knowledge/technical/pr-review-systems-map.md``.
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
from .verifier import (
    VERIFIER_PROMPT_VERSION,
    VerifierVerdict,
    verify_artifact,
    verify_finding,
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
    # Stage 2 adversarial verifier
    "VERIFIER_PROMPT_VERSION",
    "VerifierVerdict",
    "verify_artifact",
    "verify_finding",
]
