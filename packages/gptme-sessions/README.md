# gptme-sessions

Session tracking and analytics for gptme agents.

Provides an append-only JSONL-based session record system that any gptme agent can use to track operational metadata across sessions: which harness ran, what model was used, what type of work was done, and the outcome.

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
# Show stats (default)
gptme-sessions stats

# Query with filters
gptme-sessions query --model opus --since 7d
gptme-sessions query --run-type autonomous --outcome productive --json

# Append a record
gptme-sessions append --harness claude-code --model opus --outcome productive

# Run analytics (duration distribution, NOOP rates, trends)
gptme-sessions runs --since 14d

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
