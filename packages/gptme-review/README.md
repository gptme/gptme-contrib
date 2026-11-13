# gptme-review

Self-hosted PR-review engine, lifecycle tracking and transport admission.

## Request billing evidence

The request usage ledger distinguishes `openrouter_generation` (generation
metadata), `openrouter_response` (the completion response's billed `usage.cost`)
and `prompt_estimate` (not billing evidence). Generation metadata takes precedence;
a failed or lagging lookup must not erase a valid response cost. The ledger keeps
`generation_id` for later reconciliation. Missing cost remains unknown/estimated,
not a billed zero. Subscription list-price equivalents are not OpenRouter charges.

## Subscription reviewer panels

`gptme_review.panel` provides one-shot Codex, Claude Code, Grok and Copilot
transports plus `PanelReviewer`, `run_reviewer` and `panel_review`. A caller
passes a **pre-admitted** roster and the complete review prompt. The package
owns bounded transport execution, response parsing, consensus merging and
per-reviewer result rows. It refuses to label a partially failed, empty panel
as a clean review, and retains individual failure rows in `PanelReviewError`.
Transport provenance is stamped by the executor, not trusted from model output.
Lifecycle stages use the shared `gptme_review.lifecycle` context, including in
panel threads. A shared deadline is checked before starting requests and caps
each transport's timeout; exhausted joins retain per-reviewer failure rows.
Consensus usage reports both requested and effective agreement, including
rejected findings. The aggregate list-price equivalent remains unknown when
any reviewer price is unknown, rather than presenting an incomplete sum as zero.

The host still owns private-repository/provider consent, quota admission,
reviewer identities, roster selection, budget policy and ledger persistence.
Importing this module does not select reviewers or grant permission to run
one. Optional CLI transports use the current user's own installed tools and
credentials; they never copy credentials from another workspace. Grok's direct
transport reads the local CLI-owned auth file but does not refresh it.

List-price equivalents are not billed subscription spend. Pass a price table
explicitly, or load one with `load_prices(path)`. Without an explicit table/path,
prices remain unknown; the portable module never guesses a brain config path.
`choose_primary_route` accepts caller-supplied availability and budget counts;
it does not perform admission or read production ledgers. Explicit per-call
arguments override the existing `AI_REVIEW_PANEL_*` defaults.

This is an extraction seam, **not yet a portable sweep CLI**. The original host
adapter continues to own production execution until it adopts this package.

## Large-diff shard planning

`gptme_review.shard` provides the pure planning and result-merging layer used
by a review driver when a diff exceeds a single prompt's budget:

- `plan_shards(diff)` groups files by package/directory and splits oversized
  files at hunk boundaries, recomputing split-hunk anchors.
- `estimate_shard_waves(additions, deletions, changed_files)` estimates
  admission cost before fetching a diff; it is not an exact shard plan.
- `shard_context_note(shard, plan)` describes the full file inventory so each
  shard can flag cross-file inconsistencies.
- `merge_shard_findings(...)` deduplicates findings across shards;
  `merge_shard_usage(...)` preserves the weakest achieved consensus rather
  than promoting one successful shard into whole-PR evidence.

This module performs no GitHub, inference or credit-balance calls. The driver
owns execution, concurrency admission, request deadlines, credit policy and
publication. It must reject plans exceeding its reserved waves or byte caps;
planning alone does not authorize spending or establish review coverage.

The initial implementation was extracted from Bob's `ai_review_shard.py` for
use by other agents without importing a brain checkout. Bob's REVIEW-key
balance/reserve calculation remains in his host adapter, not this package.

Run package tests with `python3 -m pytest packages/gptme-review/tests` from the
repository root in its development environment.
