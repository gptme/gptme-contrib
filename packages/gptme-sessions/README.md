# gptme-sessions

Session tracking and analytics for agents. Supports trajectories from gptme, Claude Code, Codex, and Copilot.

Provides an append-only JSONL-based session record system that any agent can use to track operational metadata across sessions: which harness ran, what model was used, what type of work was done, and the outcome.

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
    harness="claude-code",
    model="opus",
    run_type="autonomous",
    category="code",
    outcome="productive",
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

# Run analytics (duration distribution, NOOP rates, trends)
gptme-sessions runs --since 14d

# Discover trajectory files across all harnesses (no store required)
gptme-sessions discover --since 7d
gptme-sessions discover --harness claude-code --signals

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
{"session_id":"a1b2c3d4","timestamp":"2026-03-04T12:00:00+00:00","harness":"claude-code","model":"opus","run_type":"autonomous","category":"code","outcome":"productive","duration_seconds":2400,"deliverables":["abc123"]}
```

## Extending

Agent-specific features (journal parsing, log extraction, backfill) should be built on top of this package by importing `SessionRecord` and `SessionStore`.

## Development

```bash
cd packages/gptme-sessions
uv run pytest tests/ -v
```
