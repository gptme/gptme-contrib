# gptme-sessions

Session tracking and analytics for AI coding agents: discover trajectories from gptme, Claude
Code, Codex, Copilot CLI and Pi, extract productivity signals (commits, file writes, errors,
token usage, cost), and keep an append-only JSONL ledger you can query, grade and audit.

**Status:** beta, actively developed. The `SessionRecord`/`SessionStore` API and the core CLI
(`sync`, `query`, `stats`, `post-session`) are stable in practice; analysis commands evolve
faster.

## Why / when to use it

Once an agent runs many sessions — often across several harnesses and models — you need to answer
questions like *which model/backend actually ships work?*, *how many sessions were no-ops?*,
*what did last week cost?* or *which session wrote this line?* `gptme-sessions` normalises each
harness's native trajectory format into one record schema (harness, provider, model, run type,
category, outcome, duration, tokens, cost, deliverables) so those questions are one command away.

Related:

- [gptme-runloops](../gptme-runloops/README.md) — runs the sessions this package records
- [gptme-dashboard](../gptme-dashboard/README.md) — shows session stats from this store in a web UI
- [gptme-usage](../gptme-usage/README.md) — model registry and cost math (optional `cost` extra)

## Install

Not published on PyPI. Install from the repository:

```bash
uv tool install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-sessions"
# or, from a clone of gptme-contrib:
uv pip install -e packages/gptme-sessions
```

Extras: `shell-parse` (tree-sitter-bash for more robust commit/heredoc detection) and `cost`
(depends on [gptme-usage](../gptme-usage/README.md), also not on PyPI — install it from the repo
first).

## Quickstart

```bash
gptme-sessions discover --since 7d    # find trajectories on this machine (read-only)
gptme-sessions sync --since 14d       # import them into the store (idempotent, deduplicates)
gptme-sessions                        # 30-day summary
gptme-sessions query --model opus --since 7d
```

The store lives in `~/.local/share/gptme-sessions/` (override with `GPTME_SESSIONS_DIR` or
`--sessions-dir`).

## Native gptme child sessions

`read_session_tree()` and signal extraction resolve native gptme children from
sibling `subagent-*/conversation.jsonl` directories in the parent's logs root.
Each child must carry an absolute `parent_logdir` in `subagent-meta.json` that
matches its parent directory. Nested children are resolved by the same identity;
missing or malformed metadata is not guessed from names or modification times.
This works for both retained per-run logs roots and legacy logs roots.

Child tokens, tools, duration and mutation classification feed the parent's
`subagent_summary`; the parent's model identity remains its own. Incomplete
children with retained transcripts are counted, not silently dropped. Existing
Claude Code flat/workflow layouts remain supported.

## Python API

```python
from pathlib import Path
from gptme_sessions import SessionRecord, SessionStore

store = SessionStore(sessions_dir=Path("state/sessions"))  # default: GPTME_SESSIONS_DIR or ~/.local/share/gptme-sessions

store.append(SessionRecord(
    harness="pi",
    provider="openai-codex",
    model="gpt-5.6-luna",
    run_type="autonomous",
    category="code",
    outcome="productive",
    stop_reason="stop",
    cost_usd=0.0004264,
    duration_seconds=2400,
    deliverables=["abc123"],
))

recent = store.query(model="gpt-5.6-luna", since_days=7)
stats = store.stats()
print(f"Success rate: {stats['success_rate']:.0%}")
```

Also exported: `post_session()` (the full record-a-session pipeline), `extract_from_path()` /
`extract_signals*()` (per-harness signal extraction), `discover_*_sessions()`, and
`normalize_model()`. Build agent-specific features (journal parsing, backfills) on top of these.

## CLI

### Recording sessions

```bash
# Record a session at the end of an agent run: extract signals, determine outcome, append
gptme-sessions post-session --harness gptme --model opus \
  --trajectory ~/.local/share/gptme/logs/2026-03-07-foo/conversation.jsonl

# Reasoning telemetry: --reasoning-profile is the semantic intent (routine|default|deep);
# --reasoning-effort is the backend-native level (free-form; filled from the trajectory if omitted)
gptme-sessions post-session --harness claude-code --model <model> \
  --reasoning-profile deep --reasoning-effort high --trajectory ~/.claude/projects/x/id.jsonl

# Or import whatever is on disk
gptme-sessions sync --since 14d
gptme-sessions sync --signals    # also extract productivity signals (slower)
gptme-sessions sync --dry-run

# Amend a record after the fact (ID prefix match)
gptme-sessions annotate a1b2c3d4 --outcome productive --add-deliverable pr#42
gptme-sessions annotate a1b2 --duration 3600 --token-count 50000
```

`post-session --harness` accepts `gptme`, `claude-code`, `codex`, `copilot-cli` and `pi`.
`append` still exists but is deprecated in favour of `post-session`/`sync`.

### Querying

```bash
gptme-sessions stats
gptme-sessions show a1b2 --json
gptme-sessions query --run-type autonomous --outcome productive --json
gptme-sessions export --format csv --category code --model opus -o sessions.csv
gptme-sessions runs --since 14d     # duration distribution, NOOP rates, trends
gptme-sessions cost --days 7 --by-model
gptme-sessions search "rate limit" --days 30
```

`--since` accepts sub-day windows and natural phrasing (`30m`, `2h`, `7d`, `2w`,
`"2 hours ago"`, `all`).

### All commands

| Group | Commands |
|-------|----------|
| Record | `post-session`, `sync`, `annotate`, `append` (deprecated), `stamp-attempt-kind` |
| Inspect | `stats` (also the default with no subcommand), `query`, `show`, `export`, `runs`, `cost`, `cost-attribution`, `search` |
| Trajectories | `discover`, `signals PATH` (productivity signals, grade, `--usage`), `transcript PATH` (normalised transcript), `replay TARGET` (terminal replay) |
| Classify & grade | `classify`, `classify-stats`, `auto-tag`, `judge`, `regrade`, `repair-grades` |
| Maintenance | `dedup` (merge duplicate records), `rotate` (archive records older than `--keep-days`, default 30) |
| Provenance | `blame` |

Run `gptme-sessions <command> --help` for options.

`judge` scores autonomous-session journal entries (`journal/YYYY-MM-DD/autonomous-session-*.md`)
for goal alignment (0.0–1.0 plus a one-line reason) with an LLM; the default model is Claude
Haiku via `ANTHROPIC_API_KEY`, and other provider-prefixed models route through gptme when it is
installed. `--update-store` writes scores back to matching records.

### Session provenance (`blame`)

`blame` answers *"which AI session produced this line / commit?"* by correlating git
author-dates with session time windows from the store.

```bash
gptme-sessions blame src/watchdog.py
gptme-sessions blame src/watchdog.py --line 42
gptme-sessions blame src/watchdog.py --limit 5 --json
gptme-sessions blame gptme/gptme-contrib#1252          # PR/issue ref; needs an authenticated gh CLI
gptme-sessions blame src/hello.py --records /path/to/session-records.jsonl
```

A runnable, self-contained demo lives in
[`examples/sessions-blame/`](examples/sessions-blame/README.md).

## Where trajectories are discovered

| Harness | Default location | Override |
|---------|------------------|----------|
| gptme | `~/.local/share/gptme/logs` | `GPTME_LOGS_DIR` |
| Claude Code | `~/.claude/projects` | `CLAUDE_HOME` (uses `$CLAUDE_HOME/projects`); extra roots via `GPTME_CC_EXTRA_PROJECTS_DIRS` |
| Codex | `~/.codex/sessions` | `CODEX_SESSIONS_DIR` |
| Copilot CLI | `~/.copilot/session-state` | `COPILOT_STATE_DIR` |
| Pi (native v3) | `~/.pi/agent/sessions` | `PI_CODING_AGENT_SESSION_DIR`, else `sessionDir` in Pi's `settings.json`, else `$PI_CODING_AGENT_DIR/sessions` |

Grok Build trajectories are parsed by the signal/transcript extractors but are not auto-discovered.

Discovery and sync never modify or delete trajectories: a record keeps the source
`trajectory_path` but does not own the file, so keep (or back up) your trajectories. For Pi,
print-mode streams and unsupported session versions are skipped with a warning rather than
recorded as false no-ops, and a session Pi is still writing is read up to its last complete line.

## Records and storage

Records are append-only JSONL (`session-records.jsonl`, one object per line); `rotate` moves old
records into monthly archive files.

```jsonl
{"session_id":"a1b2c3d4","timestamp":"2026-08-31T12:00:00+00:00","harness":"pi","provider":"openai-codex","model":"gpt-5.6-luna","run_type":"autonomous","category":"code","outcome":"productive","stop_reason":"stop","cost_usd":0.0004264,"duration_seconds":2400,"deliverables":["abc123"]}
```

Field notes:

- `session_id` is the record identity. Autonomous launchers should pass their
  existing immutable full run UUID to `post_session(session_id=...)` and carry
  the compact operator label separately as `session_label`. The label may
  repeat; never use it for joins, updates, or commit-trailer ownership.
  Legacy short-ID records remain readable without relabelling or deduplication.
  Attempt-kind stamping refuses a four-hex ID spanning distinct timestamps.
- `session_label` is optional display metadata, independent of the harness's
  native session ID and any trajectory-binding sentinel. This API does not
  allocate a second run ID or migrate existing launcher consumers.

- `model` is the requested model, normalised via an alias table (`claude-opus-4-6` → `opus`,
  `anthropic/claude-sonnet-4-5` → `sonnet`); models without an alias just lose a known provider
  prefix (`openrouter/<org>/`, `anthropic/`, `openai/`, `openai-subscription/`, `xai/`), e.g.
  `openrouter/moonshotai/kimi-k3` → `kimi-k3`. `served_model` records what the provider reported serving,
  when the trajectory includes it.
- `cost_usd` is the USD-equivalent cost reported by the harness. Under OAuth/subscription access
  it can be a nominal API-equivalent value, not an incremental charge. Missing cost is `null`; a
  reported `0.0` is kept as a real observation.
- Token usage is stored alongside the record (input, output, cache-read, cache-creation, total),
  plus `sys_prompt_tokens` (first observed prompt) and `context_peak_tokens` (largest per-call
  prompt) where the trajectory allows, so analytics don't need to reparse trajectories.

## Development

```bash
uv run pytest packages/gptme-sessions/tests/ -v
```

The Pi parser is pinned to a specific upstream Pi session contract. After upgrading Pi or
refreshing fixtures, run the drift check (downloads Pi's release source; network failures are
errors, not passes):

```bash
cd packages/gptme-sessions && uv run python3 scripts/check_pi_compat.py
```
