# gptme-action-receipts — Operator Guide

Append-only audit ledger for gptme tool actions with an optional scope-check gate.
For installation and receipt format details, see the
[plugin README](../../plugins/gptme-action-receipts/README.md).

---

## Phase 1 — Ledger

Every tool execution emits one JSON line to `~/.local/share/gptme/receipts.jsonl`
before the tool runs. The plugin must be loaded via `gptme.toml`:

```toml
[plugin.action_receipts]
```

No further configuration is needed for Phase 1.

---

## Phase 2 — Scope Gate

The scope gate intercepts specific high-impact shell commands (merge, force-push,
repo/release delete) and either warns or blocks them if the target repository is
not in the operator's allowlist.

### How it works

1. After writing the receipt, the gate pattern-matches the shell command.
2. If a sensitive action is detected, it extracts the target repository via
   `--repo` flag, positional argument, or workspace `git remote origin`.
3. The target is checked against the configured allowlist using `fnmatch` glob
   patterns.
4. If not authorized:
   - **warn mode** (default): logs a warning; the tool executes normally.
   - **block mode**: raises `StopPropagation`; the tool is aborted.

The gate is fail-open: any config parse error or extraction failure logs a
warning and falls back to "no violation". The agent is never crashed by an
audit failure.

### Gated actions

| Scope key | Trigger command |
|---|---|
| `merge_repos` | `gh pr merge` |
| `force_push_repos` | `git push --force` (or `-f`) |
| `repo_delete` | `gh repo delete` |
| `release_delete` | `gh release delete` |

Only `shell`, `bash`, and `execute` tool calls are inspected. File-write tools
(`save`, `append`, `patch`) are not gated.

### Configuration

The gate reads `~/.config/gptme/scope.yaml` by default. Override the path with
`GPTME_SCOPE_MANIFEST=/path/to/scope.yaml`.

If the config file does not exist, all allowlists are empty and
`violation_action` defaults to `warn`.

#### scope.yaml schema

```yaml
version: 1

# 'warn'  — log a warning, allow the action (default; safe for soak periods)
# 'block' — abort the action; the agent sees "[scope-gate] BLOCKED: ..."
violation_action: warn

scopes:
  # Repos where 'gh pr merge' is authorized
  merge_repos:
    - ErikBjare/bob          # exact match
    - ErikBjare/*            # wildcard: all repos under ErikBjare

  # Repos where 'git push --force' is authorized
  force_push_repos:
    - ErikBjare/bob

  # Repos where 'gh repo delete' is authorized (leave empty to deny all)
  repo_delete: []

  # Repos where 'gh release delete' is authorized
  release_delete: []
```

Patterns follow Python `fnmatch` rules: `*` matches any sequence of characters
**including** `/`, so `ErikBjare/*` matches `ErikBjare/bob` but not `other/repo`.
A bare `*` authorizes **every** repository — use it only if that is intentional.

#### Worked example — single-agent allowlist

An agent permitted to self-merge PRs in its own brain repo and force-push its
own PR worktrees, but nothing else:

```yaml
version: 1
violation_action: warn

scopes:
  merge_repos:
    - ErikBjare/bob          # agent's brain repo

  force_push_repos:
    - ErikBjare/bob          # rebase fixup pushes on own branches

  repo_delete: []
  release_delete: []
```

The `gptme/gptme-contrib#1175` incident (unauthorized `gh pr merge 1175 --squash
--repo gptme/gptme-contrib`) would have triggered `merge_repos` here because
`gptme/gptme-contrib` is absent from `merge_repos`. In warn mode a log line
appears; in block mode the command is aborted before execution.

---

### Violation log interpretation

**Warn mode** — search gptme's log output for:

```txt
action-receipts: SCOPE VIOLATION (warn only): action 'merge_repos' on 'gptme/gptme-contrib' not in allowlist ['ErikBjare/bob']
```

Fields:
- `action` — which scope key triggered (`merge_repos`, `force_push_repos`, etc.)
- `on` — the repository the command targeted
- `not in allowlist` — the configured allowlist at the time

**Block mode** — the agent's tool call returns an error:

```txt
[scope-gate] BLOCKED: action 'merge_repos' on 'gptme/gptme-contrib' not in allowlist ['ErikBjare/bob']
```

The same information appears in the log at `WARNING` level.

To scan historical violations from the receipts ledger, use:

```bash
# All SCOPE VIOLATION entries today
journalctl --user --since today -g "SCOPE VIOLATION"

# Or if gptme writes to a log file:
grep "SCOPE VIOLATION" ~/.local/share/gptme/gptme.log
```

---

### Warn → block mode transition

Run in `violation_action: warn` for at least 7 days before switching to `block`.
The soak period confirms that all legitimate automation is covered by the
allowlist before any command is hard-blocked.

**Transition checklist:**

1. Run in warn mode for 7+ days of normal autonomous operation.
2. Collect all `SCOPE VIOLATION` log lines:
   ```bash
   journalctl --user --since "7 days ago" -g "SCOPE VIOLATION" | \
     grep "action-receipts:" | sort | uniq -c | sort -rn
   ```
3. For each violation, decide:
   - **False positive** (legitimate action): add the repo to the allowlist.
   - **True positive** (unauthorized action): leave it out; block mode will stop it.
4. Update the allowlist in `scope.yaml`.
5. Set `violation_action: block`.
6. Verify with a dry-run (inspect log for unexpected block events in the first session).

**Reversing a block** — if a legitimate command gets blocked unexpectedly, add
the repo to the allowlist and reload the agent (the config is re-read on every
tool call; no restart required).

---

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `GPTME_SCOPE_MANIFEST` | `~/.config/gptme/scope.yaml` | Override scope config path |
| `GPTME_RECEIPTS_LEDGER` | `~/.local/share/gptme/receipts.jsonl` | Override ledger path |
| `GPTME_SESSION_ID` | `"unknown"` | Session ID used in receipts |
| `GPTME_MODEL` | `"unknown"` | Model attribution in receipts; falls back to `CC_MODEL` |
