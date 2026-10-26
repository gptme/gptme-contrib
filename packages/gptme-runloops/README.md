# gptme-runloops

A run-loop framework for operating AI agents autonomously on a schedule: lock-protected
autonomous sessions, GitHub PR/issue monitoring, email handling and team coordination, executed
on interchangeable backends (gptme, Claude Code, Codex, Grok Build), plus a one-shot,
resumable `run` command for any backend.

**Status:** experimental, in production use by the gptme agent fleet. The generic pieces
(`run`, `select`, the backend executors, `BaseRunLoop`, `review`) are reusable as-is; the
`autonomous`, `monitoring`, `run-item` and `team` loops assume a
[gptme-agent-template](https://github.com/gptme/gptme-agent-template)-style workspace and are
more opinionated.

## Why / when to use it

An agent that runs unattended needs more than a cron line: it needs a lock so runs don't overlap,
backoff when the same work keeps failing, a git pull before starting, structured logs, and a way
to swap the underlying agent CLI without rewriting the loop. `gptme-runloops` provides those as a
`BaseRunLoop` class and a CLI you schedule with cron or a systemd timer.

It composes with other contrib packages — use them, or bring your own task and communication
systems:

- [gptodo](../gptodo/README.md) — task management; the `team` loop's coordinator delegates
  through it
- [gptmail](../gptmail/README.md) — the `email` loop syncs and answers mail through it
- [gptme-sessions](../gptme-sessions/README.md) — analyse the sessions these loops produce
- [gptme-coordination](../gptme-coordination/README.md) — work claims between concurrent agents

## Install

Not published on PyPI. Install from the repository:

```bash
uv tool install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-runloops"
# or, from a clone of gptme-contrib:
uv pip install -e packages/gptme-runloops
```

The backend CLIs (`gptme`, `claude`, `codex`, `grok`) and `gh` are external and must be on
`PATH` for the backends/loops that use them. The console script is `gptme-runloops`
(`run-loops` is a deprecated alias).

> **Safety:** the loops run their backend non-interactively with permission prompts bypassed
> (e.g. Claude Code with `--dangerously-skip-permissions`). Run them under an account and in a
> workspace where that is acceptable.

## Quickstart: one-shot runs on any backend

```bash
# Run one prompt; prints one JSON line: backend, model, session_id, result,
# exit_code, is_error, resumed, timed_out, cost_usd, agent_output
gptme-runloops run --workspace . --backend claude-code --allowed-tool Read "summarise TODO.md"

# Continue that session (same backend + model)
gptme-runloops run --workspace . --backend claude-code --model <model> --resume <session_id> "and now?"
```

Resume mapping: claude-code `--resume <id>`, codex `codex exec resume <id>`, gptme `--name <id>`.
Backends without resume support (grok-build) fail with a usage error rather than silently
starting fresh. A rejected resume id is an error unless `--on-resume-failure fresh` opts into a
new session (optionally with `--fallback-prompt-file` to carry context over). Tool restriction is
backend-native: `--allowed-tool` (claude-code `--allowedTools`, gptme `--tools`) or `--sandbox`
(codex). `--report` (claude-code only) adds `report_text`, every assistant text turn joined: `result`
is only the final message, which a delegated run can reduce to a closing line ("report above").
Other options: `--prompt-file`, `--timeout` (default 1800 s); `--backend` defaults to
`$AGENT_BACKEND`, else `gptme`. Exit code is 0 on success, 1 on an error result, 2 on usage errors.

## Commands

| Command | Description |
|---------|-------------|
| `run [PROMPT]` | One prompt on any backend, JSON result (see above) |
| `select [--config PATH] [--state-dir DIR] [--json]` | Print the first viable `backend`/`model` from `harness-quota.toml`, skipping candidates whose binary is missing or that are blocked in the block registry. Exit 1 if none |
| `autonomous` | One autonomous session. Prompt from `scripts/runs/autonomous/autonomous-prompt.txt` in the workspace, else a built-in fallback |
| `monitoring [--org ORG]... [--repo OWNER/REPO]... [--author LOGIN] [--agent-name NAME]` | GitHub project monitoring: find PRs/issues needing action (CI failures, review comments, merge eligibility) and act on them. Defaults to the `claude-code` backend |
| `run-item --workspace DIR [--work-file F] ...` | Execute a single project-monitoring work item (`--dry-run` prints the execution plan) |
| `email` | Sync mail (`mbsync -a`), then answer unreplied messages using [gptmail](../gptmail/README.md) |
| `team [--tools LIST]` | Coordinator agent with restricted tools that delegates work to subagents via gptodo |
| `review [--working-tree \| --base SHA --head SHA] [--output F] [--model M]` | LLM review of a local diff, emitted as `ReviewArtifact` JSON (no forge access needed) |
| `review-pr OWNER/REPO#NUM [--shadow\|--publish] [--min-confidence X]` | Review a GitHub PR; `--publish` posts findings as inline comments (default is shadow mode) |

`autonomous`, `email`, `team` and `monitoring` accept `--workspace` (default: current
directory), `--model`, `--tool-format markdown|xml|tool` and `--backend`.

`harness-quota.toml` (default `~/.config/gptme/harness-quota.toml`):

```toml
[[candidates]]
backend  = "claude-code"
model    = "claude-sonnet-4-6"
priority = 1

[[candidates]]
backend  = "gptme"
model    = "openrouter/deepseek/deepseek-chat-v3-0324:free"
priority = 2
```

## Review artifact output

`review` and `review-pr` now write **canonical core v2 JSON**, both on stdout
and with `--output`. Consumers can load it directly with
`gptme.util.review.ReviewArtifact.from_json()`; no forge-neutral reader is needed
for these new outputs. Local reviews use PR number zero. Unsafe or unknown
merge verdicts still exit nonzero, even when the artifact was written successfully.

The internal Python `pr_review.schema.ReviewArtifact` remains a v1 Pydantic model
for the verifier and publisher during migration. Its `model_dump_json()` still
serializes that internal shape; use `pr_review.core_compat.artifact_json()` or
`save_artifact()` for the public wire format. Consumers previously using
`schema.ReviewArtifact.model_validate_json()` on CLI output must migrate to the
core reader (or read the v2 dictionary).

V2 output retains actionable severity, finding identity (`independence_key`),
category, open/dropped status, merge safety, target kind, and diff fingerprint.
Run-local metadata such as summary, model, confidence, and timestamps is not in
this wire format; the returned Python object still carries it. A confirmed
*defect* stays `status: "open"`; core's `"confirmed"` means a verified *fix*.
This output migration does not yet replace internal models or remove legacy
readers and conversion tables.

## Python API

```python
from pathlib import Path
from gptme_runloops import AutonomousRun, get_executor, list_backends

print(list_backends())  # ['claude-code', 'codex', 'gptme', 'grok-build']
run = AutonomousRun(Path("."), executor=get_executor("claude-code"))
exit_code = run.run()
```

Write your own loop by subclassing `BaseRunLoop` and overriding `generate_prompt()` (plus
optionally `has_work()`, `pre_run()`, `post_run()`). The base class provides the lock (in
`<workspace>/logs/`), git pull with retry, timeout handling, logging, and opt-in
consecutive-failure backoff.

Other modules:

- `gptme_runloops.utils.executor` — backend registry (`get_executor`, `list_backends`):
  `execute()` for loops, `run_once()` for one-shot/resumable runs
- `gptme_runloops.gates` — composable runtime-admission gates ("does this triggered session
  deserve to spend inference?"), as pure functions
- `gptme_runloops.triggers` — composable trigger gates ("should a scheduled slot fire at all?")
- `gptme_runloops.pr_review` — versioned, forge-neutral `ReviewArtifact`/`Finding` schema, reviewer,
  verifier, GitHub adapter and a golden corpus for evaluating reviewer models
- `gptme_runloops.pm_dispatch`, `merge_lifecycle`, `pm_bandit`, `worker_records`,
  `prompt_templates` — project-monitoring internals (lane dispatch, PR merge state machine,
  session records)
- `gptme_runloops.utils` — `lock`, `github`, `git`, `logging`, `prompt`, `state`

The legacy import path `run_loops` is kept as a symlink to `gptme_runloops` in source checkouts.

## Contributor initial reviews (PM)

`contributor_unreviewed` is a slow-lane `pr-review` session, never an
`automated-pr-review` or code-repair session. `RunItem` normalizes mixed groups
to this type **before** merge-lifecycle I/O; the shared coordination claim is
`github:OWNER/REPO#NUMBER`. The dedicated prompt replaces generic monitoring,
repair, arc and mention instructions. It requires live PR/head/CI/managed-review
checks, detached isolated-worktree inspection, safe test execution, touched and
adjacent regression suites, and a final head/eligibility recheck. The session
submits one review with explicit `commit_id`: approval only after meaningful
green verification, actionable changes requested for defects, or a comment
review for infrastructure/verification limits. It never edits or pushes the
contributor branch, opens a parallel fix, or merges.

The legacy `ProjectMonitoringRun` discovery loop also uses this shared review-only
prompt. It executes each contributor review in a separate session, excludes those
PRs (including other event types for the same PR) from its generic repair batch,
and retains unrelated repair work. At discovery, existing notifications/activity
for a PR take priority over its initial-review candidate, so consumed legacy
watermarks never hide an unanswered request behind a review-only session.
Per-session recording retains each session's own outcome; a separate aggregate
result reports batch failure. A timed-out review lookup skips that candidate
without abandoning later PRs in the repository scan.

The detector owns age/author/green-CI eligibility and full-head-SHA dedupe; its
managed-login configuration must be carried as exactly one
`managed_logins=Login1,Login2` token in item `detail`. The handler renders quoted
JSON evidence and adds the authenticated viewer; user logins and GitHub App
`[bot]` logins are accepted. Missing, duplicate or malformed tokens fail closed,
and display/operator names are never inferred as logins.
`CONTRIBUTOR_REVIEW_ONLY_V1` in `run_item.py` marks the safe
executor capability, not deployment of a shell fallback. Enable the detector
only after the actual executor and any shell adapters support the same policy.
Shell deployments must normalize mixed types before cheap self-merge pre-gates,
lifecycle, prompt concatenation, coordination claims and post-session wait/merge.
Do not route this type through the retired `review-pr` intercept.

PR state is observed, but movement or issue comments do not count as a review.
`worker_records.viewer_review_since(repo, number, since_iso, *, cwd, runner=...)`
returns `True` only for a newly submitted authenticated-viewer review on the
live head (`APPROVED`, `CHANGES_REQUESTED` or `COMMENTED`), `False` if absent,
and `None` if unobservable. It paginates reviews, checks head stability between
pages, and is also suitable for a shell adapter. Review-only post-session effect
uses this evidence instead of generic reply checks or fallback comments.
It does not promote pending PR activity or notification watermarks (including
immutable launch snapshots): co-grouped work suppressed by the review-only
prompt must remain eligible for a later handling session. The default launcher
preserves item `detail`, including the required managed-login evidence.
Review-only sessions also skip generic notification-handling latency receipts
and PR-movement deliverable attribution: neither a suppressed mention nor a
contributor's concurrent commit is work delivered by the reviewer. If a review
is absent or unobservable (including a failed worker), launch event fingerprints
are cleared for retry without consuming suppressed watermarks. A confirmed
prompt-mandated skip (missing/invalid managed-login evidence, draft,
closed/merged, managed-author, non-green CI, or a managed review already on the
live head) is graded `observed` so recovery does
not drain the ineffective budget, and fingerprints are still cleared so other
slot types and later eligibility are not suppressed. Head movement is not a
skip: the new head remains eligible and retries. `contributor_review_skip_observed`
is the observation helper for that distinction; an empty parsed managed-login
set proves the local fail-closed skip without a GitHub request. Dispatch ledger
`types` likewise reflect only the review; mixed inputs remain available as
per-item `original_types`, never credited as completed repairs.

## Development

```bash
uv run pytest packages/gptme-runloops/tests/
```

## License

MIT
