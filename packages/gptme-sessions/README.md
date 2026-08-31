# gptme-sessions

Session tracking and analytics for agents. Supports trajectories from gptme,
Claude Code, Codex, Copilot, Grok Build, and Pi native v3 sessions.

Provides an append-only JSONL-based session record system that any agent can use
to track operational metadata across sessions: which harness and inference
provider ran, what model was used, what type of work was done, and the outcome.

## Installation

```bash
pip install gptme-sessions
```

## Usage

### Python API

```python
from pathlib import Path
from gptme_sessions import SessionRecord, SessionStore

# Create a store (defaults to ./state/sessions/)
store = SessionStore(sessions_dir=Path("state/sessions"))

# Append a session record
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

# Query records
recent = store.query(model="opus", since_days=7)

# Get stats
stats = store.stats()
print(f"Success rate: {stats['success_rate']:.0%}")
```

### CLI

```bash
# Show stats (default — auto-falls back to discover if store is empty)
gptme-sessions stats

# Show details for a single session by ID (or prefix)
gptme-sessions show a1b2c3d4
gptme-sessions show a1b2 --json

# Query with filters
gptme-sessions query --model opus --since 7d
gptme-sessions query --run-type autonomous --outcome productive --json

# Structured export (JSON or CSV) — backups, audit trails, data portability
gptme-sessions export --format json --since 7d
gptme-sessions export --format csv --category code --model opus -o sessions.csv

# --since accepts sub-day windows and natural phrasing (units: s, m, h, d, w).
# Sub-day windows filter precisely (no rounding up to a whole day).
gptme-sessions query --since 2h               # last 2 hours
gptme-sessions query --since 30m --stats      # last 30 minutes
gptme-sessions query --since "2 hours ago"    # same as 2h
gptme-sessions query --since all              # no time filter

# Run analytics (duration distribution, NOOP rates, trends)
gptme-sessions runs --since 14d

# Discover trajectory files across all harnesses (no store required)
gptme-sessions discover --since 7d
gptme-sessions discover --harness claude-code --signals
gptme-sessions discover --harness pi --signals

# Import discovered sessions into the store (safe to re-run — deduplicates)
gptme-sessions sync --since 14d
gptme-sessions sync --signals  # extract productivity signals (slower)
gptme-sessions sync --dry-run  # preview what would be imported

# Annotate an existing session record (amend fields after the fact)
gptme-sessions annotate a1b2c3d4 --outcome productive --add-deliverable pr#42
gptme-sessions annotate a1b2 --duration 3600 --token-count 50000

# Score recent sessions with an LLM judge (goal-alignment rating 1–5)
gptme-sessions judge
gptme-sessions judge --last 5
gptme-sessions judge --update-store  # write scores back to the store

# Record a session at the end of an agent run (full pipeline)
gptme-sessions post-session --harness gptme --model opus \
  --trajectory ~/.local/share/gptme/logs/2026-03-07-foo/conversation.jsonl

# Append a record manually (deprecated: prefer post-session or sync)
gptme-sessions append --harness claude-code --model opus --outcome productive

# Custom sessions directory
gptme-sessions --sessions-dir /path/to/state/sessions stats
```

Pi discovery recursively scans native v3 tree sessions under
`PI_CODING_AGENT_SESSION_DIR`, or `$PI_CODING_AGENT_DIR/sessions` and then
`~/.pi/agent/sessions` when no direct override is set. Print-mode streams and
unsupported native versions are visibly warned and skipped instead of being
imported as false NOOPs. For a session Pi is actively appending, discovery uses
the last complete newline-delimited prefix; a transient partial tail cannot hide
that session or abort discovery of its siblings.

Discovery and sync are non-mutating. A synced record retains the source
`trajectory_path`; it does not copy or own the trajectory. Pi session JSONL is
a historical artifact, so keep or independently back up the source tree—do not
delete it after sync.

## Model Normalization

Model names are automatically normalized to short canonical forms:

| Input | Normalized |
|-------|-----------|
| `claude-opus-4-6` | `opus` |
| `anthropic/claude-sonnet-4-5` | `sonnet` |
| `openrouter/anthropic/claude-haiku-4-5` | `haiku` |
| `gpt-5.3-codex` | `gpt-5.3-codex` |

## Storage Format

Records are stored as append-only JSONL (one JSON object per line):

```jsonl
{"session_id":"a1b2c3d4","timestamp":"2026-08-31T12:00:00+00:00","harness":"pi","provider":"openai-codex","model":"gpt-5.6-luna","run_type":"autonomous","category":"code","outcome":"productive","stop_reason":"stop","cost_usd":0.0004264,"duration_seconds":2400,"deliverables":["abc123"]}
```

`cost_usd` is the USD-equivalent cost reported by the harness. With OAuth or
subscription access it can be a nominal API-equivalent value rather than an
incremental charge on the subscription invoice. Missing cost is `null`; a
reported `0.0` is retained as a real observation.

## Extending

Agent-specific features (journal parsing, log extraction, backfill) should be built on top of this package by importing `SessionRecord` and `SessionStore`.

## Development

```bash
cd packages/gptme-sessions
uv run pytest tests/ -v
```
