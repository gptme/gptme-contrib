# gptme-action-receipts

Append-only audit ledger for gptme tool actions. Before each tool executes,
this plugin emits a hashed receipt to `~/.local/share/gptme/receipts.jsonl`.

## Motivation

The [gptme-contrib#1175 unauthorized self-merge incident](https://github.com/gptme/gptme-contrib/issues/1175)
would have been caught at a pre-action gate: the merge action was out of operator
scope, and a receipt system forces the agent to record scope before executing.
This plugin provides the audit trail.

## Receipt format

```json
{
  "session_id": "ses-abc123",
  "model": "claude-sonnet-4-6",
  "action_type": "shell",
  "target": "gh pr merge --squash 625",
  "workspace": "/home/bob/projects/gptme",
  "timestamp": "2026-07-04T18:00:00+00:00",
  "receipt_hash": "sha256:abc123..."
}
```

The `target` field contains the first 512 characters of the tool's content (e.g., shell command for shell tools, file content for write operations, etc.). Truncation keeps ledger lines compact.

The `receipt_hash` is a deterministic SHA-256 digest of the receipt fields. It
can detect accidental corruption of a receipt line, but it is not an adversarial
tamper-proofing mechanism: a writer with access to the ledger can modify a line
and recompute the hash. Harder deletion/rewriting resistance is future work.

## Usage

### Via gptme.toml (recommended)

```toml
[plugin.action_receipts]
# no options needed — the plugin self-registers on load
```

### Manual registration

```python
from gptme_action_receipts import register
register()
```

## Configuration

| Env var | Default | Description |
|---|---|---|
| `GPTME_RECEIPTS_LEDGER` | `~/.local/share/gptme/receipts.jsonl` | Override ledger path |
| `GPTME_SESSION_ID` | `"unknown"` | Session ID fallback when not in a gptme session |
| `GPTME_MODEL` | `"unknown"` | Model attribution; falls back to `CC_MODEL` if unset |

## Ledger inspection

```bash
# View last 10 receipts
tail -10 ~/.local/share/gptme/receipts.jsonl | python3 -m json.tool

# Find all shell actions in a session
jq 'select(.action_type == "shell" and .session_id == "ses-abc123")' \
  ~/.local/share/gptme/receipts.jsonl

# Count actions by type
jq -r '.action_type' ~/.local/share/gptme/receipts.jsonl | sort | uniq -c | sort -rn
```

## Roadmap

- **Phase 1 (this plugin)**: Ledger + receipt emission. No blocking gate.
- **Phase 2**: Scope-check gate — abort out-of-scope actions before execution.
  See [gptme/gptme#2547](https://github.com/gptme/gptme/issues/2547) for the
  community schema discussion.
