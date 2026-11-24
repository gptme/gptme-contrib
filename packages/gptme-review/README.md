# gptme-review

Self-hosted PR-review engine, lifecycle tracking and transport admission.

## Install

The package is a library with no console script. It depends on the workspace
packages `bobutils`, `gptme-block-registry` and `gptme-contrib-lib`, so install it
from a gptme-contrib checkout (Python 3.11+):

```bash
uv sync --package gptme-review
uv run python -c "import gptme_review"
```

## Modules

| Module | What it holds |
|--------|---------------|
| `core` | Shared reviewer pieces: the one-shot `llm` engine, finding normalisation (severity, class, summary-only), `confidence_score`, merge/disposition verdicts, subscription-model availability |
| `writer` | Per-PR review writer: marker comment, publish, threads, disposition, rendering, checkout and preflight. I/O resolves at call time through `bind_io`, so a host injects its own GitHub access and identity |
| `routing` | Transport admission: which reviewer (OpenRouter or a subscription CLI) may serve a request, consensus admission across distinct model families |
| `panel` | One-shot subscription transports (Codex, Claude Code, Grok, Copilot) and multi-model panels; see below |
| `lifecycle` | Append-only, best-effort review lifecycle ledger; `python -m gptme_review.lifecycle` prints a read-only per-run JSON summary |
| `sweep` | Sweep candidate selection, marker policy and bounded dispatch; see below |
| `sweep_outcome` | Classifies one sweep attempt: quota exhaustion vs. broken reviewer, superseded head, deferred review, failure counts |
| `sweep_fallback` | Retries selected primary-engine failures on a fallback engine, skipping heads already reviewed and diffs too large to retry |
| `shard` | Shard planning for diffs larger than one review prompt; see below |

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

Each CLI call gets a fresh mode-0700 working directory under the current user's
home (which must be trusted and writable). Both the subprocess cwd and any CLI
cwd flag point there. Scratch is not placed under shared `/tmp`: ancestor
instruction discovery could still load `/tmp/CLAUDE.md` or `/tmp/AGENTS.md` from
an otherwise private child directory. Claude disables settings sources; Grok's
prompt file stays inside the private directory. Scratch is removed on success
and failure; CLI-owned audit trajectories outside it remain enabled. This
isolates shared-directory injection, not the user's own trusted home/global
configuration or administrator-managed policy.

List-price equivalents are not billed subscription spend. Pass a price table
explicitly, or load one with `load_prices(path)`. Without an explicit table/path,
prices remain unknown; the portable module never guesses a brain config path.
`choose_primary_route` accepts caller-supplied availability and budget counts;
it does not perform admission or read production ledgers. Explicit per-call
arguments override the existing `AI_REVIEW_PANEL_*` defaults.

Hosts can pass a private transport mapping to `run_reviewer(..., transports=...)`
and a `ReviewerRunner` callback to `panel_review(..., runner=...)`. The runner
receives the reviewer, prompt, deadline-capped timeout and price table, and must
return a `ReviewerResult` (including failure results). If a runner raises an
`Exception`, the panel records a failed result with the exception type, message
and measured runner duration, rather than reporting a deadline abandonment.
A host wrapper can call
`run_reviewer` with its own transport mapping, preserving local policy and test
seams without mutating the package's globals. An explicit empty mapping disables
all transports for that call; it does not fall back to the built-in table.
Omitting these arguments retains the package defaults, resolved at call time.

The original host adapter continues to own production panel execution until it
adopts this package.

## Sweep candidate selection

`gptme_review.sweep` decides which open PRs still need a review. The host
passes an explicit `SweepIdentity` (login + marker namespace), a `SweepConfig`
(authors, consensus-upgrade repos, min-age), and a `SweepHost` (GitHub listing,
comments, mergeability, outage probe, reactions, file/patch fetch). The package
has no default agent login and does not read a brain checkout.

`select_candidates(repo, config, host, now)` returns the dispatchable PRs, or
`None` when listing failed (distinct from an empty list). `SweepHost.pr_comments`
also distinguishes failure (`None`) from an empty comment list: an unreadable PR
is skipped for that pass, not admitted as never reviewed. Marker parsing and
`@login review` triggers are identity-scoped, so two reviewers on the same PR
do not consume each other's markers. Duplicate-PR tagging, consensus-upgrade
eligibility, and trusted-request opt-in live here.

### Bounded dispatch

`dispatch_reviews(selected, host, workers=1, started=None, clock=...)` schedules
an already-selected queue. Its `DispatchHost` has three callbacks:

- `admission(pr, elapsed, already_started)` returns `DispatchAdmission()`,
  `DispatchAdmission(reason)` to stop, or `DispatchAdmission(reason, defer=True)` to skip one oversized
  PR without starving smaller PRs. It runs immediately before each submission,
  even when the worker pool has idle slots. The host owns deadline, shard-wave,
  credit-reserve and first-review-exception policy.
- `run_review(pr)` executes on a worker. The host owns command construction,
  subprocess timeout/cleanup, result classification, durable attempts/failures,
  alerts, reactions and optional self-merge follow-through.
- `stop_reason()` returns a breaker reason or `None` on the scheduler thread.
  Once observed, a stop drains existing workers without new submissions. Breakers
  are polled before and after admission, not atomically with submission: a host
  needing strict spend reservations must enforce them in execution too. Hosts
  must synchronize mutable state shared with their workers.

The scheduler bounds concurrency and considers each `(repo, number)` only once
per dispatch call, independent of worker timing. The first selected entry wins,
including when deferred or when later entries list a different head; execution
still owns head-freshness checks, and a later sweep can retry. Duplicate entries
are classified before admission, even in a stopped queue's tail, and returned in
`DispatchResult.duplicates` without invoking host admission/execution for them.
Unexpected callback exceptions propagate; they do not become clean outcomes.
`DispatchResult` reports scheduling, not review successes or merge evidence:
`attempted + len(size_deferred) + len(budget_skipped) + len(duplicates)` equals
`len(selected)` on a normal return.
Pass the sweep's original monotonic `started` to charge candidate-listing time,
and pass a host clock explicitly when maintaining local test/clock seams.

This is the first dispatch extraction, not a turnkey production driver. The
host still owns subprocess review execution, fallback routing, outcome accounting,
alerts, self-merge and REVIEW-key budget. Dry-run callers must not invoke the
side-effecting dispatcher. The CLI remains candidate listing only:

```txt
python -m gptme_review.sweep --login reviewer-bot --marker-namespace agent-ai-review \
  --repos owner/repo --authors reviewer-bot --dry-run
```

`--login` and `--marker-namespace` are required. This is candidate listing, not
a production sweep: it does not run reviews. If every repository listing fails,
the CLI exits non-zero rather than reporting an empty successful sweep. Partial
listing failures are reported on stderr while available repositories still run.

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
