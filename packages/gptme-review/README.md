# gptme-review

Self-hosted PR-review engine, lifecycle tracking and transport admission.

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
