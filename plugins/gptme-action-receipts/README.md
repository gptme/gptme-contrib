# gptme-action-receipts — audit ledger and scope gate for agent tool calls

Records a hashed, append-only receipt of every gptme tool call *before* it runs,
and checks high-impact shell commands (`gh pr merge`, `git push --force`,
`gh repo delete`, `gh release delete`) against an operator allowlist of
repositories.

**Status:** experimental. The ledger format is stable. The scope
gate is reliable in `warn` mode only — see [Known issues](#known-issues) before
relying on `block`.

## Why

Autonomous agents occasionally do things nobody authorized — e.g. the
[gptme-contrib#1175](https://github.com/gptme/gptme-contrib/issues/1175)
unauthorized self-merge. This plugin gives you:

1. **An audit trail** — "what did the agent do, in which session, with which
   model, in which workspace" as one JSON line per action.
2. **A pre-action gate** — sensitive commands are checked against a
   `scope.yaml` allowlist before execution, so an out-of-scope merge is flagged
   in the log.

It is a pure hook plugin (no tools are added). Pairs well with
[gptme-hooks-examples](../gptme-hooks-examples/README.md) if you want to write
your own `TOOL_EXECUTE_PRE` hooks.

## Install

Not published on PyPI. Install into the same environment as gptme:

```bash
pip install "gptme-action-receipts @ git+https://github.com/gptme/gptme-contrib.git#subdirectory=plugins/gptme-action-receipts"
```

The package registers a `gptme.plugins` entry point, so once installed it loads
automatically. If you use a `[plugins] enabled` allowlist, add
`action_receipts` to it.

Alternatively, load it from a gptme-contrib checkout without installing
(PyYAML must be available):

```toml
# gptme.toml
[plugins]
paths = ["path/to/gptme-contrib/plugins/gptme-action-receipts"]
```

From Python, `from gptme_action_receipts import register; register()` registers
the hook manually.

## Receipts

Each tool call appends one line to `$XDG_DATA_HOME/gptme/receipts.jsonl`
(default `~/.local/share/gptme/receipts.jsonl`):

```json
{
  "session_id": "ses-abc123",
  "model": "claude-sonnet-4-6",
  "action_type": "shell",
  "target": "gh pr merge --squash 625",
  "workspace": "/path/to/workspace",
  "timestamp": "2026-07-04T18:00:00+00:00",
  "receipt_hash": "sha256:abc123..."
}
```

- `target` is the first 512 characters of the tool content (e.g. the shell
  command). For file-writing tools (`save`, `append`, `patch`,
  `patch_anchored`, `morph`) it is the **file path**, not the file body.
- `receipt_hash` is a deterministic SHA-256 over the other fields. It detects
  accidental corruption of a line; it is **not** tamper-proof — anyone who can
  write the ledger can rewrite a line and recompute the hash.
- Write failures are logged and never crash the agent.

Inspecting the ledger:

```bash
tail -10 ~/.local/share/gptme/receipts.jsonl | jq .
jq 'select(.action_type == "shell" and .session_id == "ses-abc123")' ~/.local/share/gptme/receipts.jsonl
jq -r '.action_type' ~/.local/share/gptme/receipts.jsonl | sort | uniq -c | sort -rn
```

## Scope gate

After writing the receipt, shell-type tool calls (`shell`, `bash`, `execute`)
are matched against the gated commands. The target repo is taken from `--repo`,
a positional argument, or the workspace's git remote, and checked against
`fnmatch` patterns in the scope manifest:

```yaml
# ~/.config/gptme/scope.yaml  (template: examples/scope.yaml)
version: 1
violation_action: warn   # 'warn' (default) or 'block' — see Known issues
scopes:
  merge_repos: [my-org/my-agent]     # gh pr merge
  force_push_repos: [my-org/*]       # git push --force / -f
  repo_delete: []                    # gh repo delete
  release_delete: []                 # gh release delete
```

The gate is fail-open: a missing or unparsable manifest, or a repo it can't
resolve, never blocks. With no manifest every allowlist is empty, so in warn
mode each gated command whose repo it resolves logs a `SCOPE VIOLATION`
warning (an unresolvable repo is skipped silently). Run in `warn` for a while
and add legitimate repos to the allowlist.

The [operator guide](../../docs/plugins/gptme-action-receipts.md) covers the
warn → block transition and log interpretation in detail.

## Configuration

| Env var | Default | Description |
|---|---|---|
| `GPTME_RECEIPTS_LEDGER` | `$XDG_DATA_HOME/gptme/receipts.jsonl` | Ledger path override |
| `GPTME_SCOPE_MANIFEST` | `~/.config/gptme/scope.yaml` | Scope manifest path override |
| `GPTME_SESSION_ID` | `unknown` | Session ID fallback when gptme doesn't provide one |
| `GPTME_MODEL` | `unknown` | Model attribution; falls back to `CC_MODEL` |

## Limitations

- File-write tools are recorded but not gated.
- Only the four command families above are gated; anything else is
  recorded only.
- The ledger is append-only by convention, not enforced.

## Known issues

- **`block` mode does not currently stop the command.** gptme's
  `TOOL_EXECUTE_PRE` hooks cannot cancel a tool call (`StopPropagation` only
  skips lower-priority hooks), and the hook constructs
  `StopPropagation(<message>)` although that class takes no arguments, so the
  resulting error is swallowed by gptme's hook runner. In practice a violation
  under `block` is not blocked and no warning is logged (the receipt is still
  written); keep `violation_action: warn`
  until this is fixed.
