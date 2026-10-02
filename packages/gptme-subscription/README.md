# gptme-subscription

Quota-aware rotation between multiple Claude Code subscription credentials
("slots"): watch the 5-hour and weekly usage limits, decide when to move load to
another slot, and flip the live `~/.claude/.credentials.json` symlink safely.
Ships a `gptme-subscription` CLI and a Python API.

**Status:** alpha. Used in production by long-running autonomous agents, but the
API and CLI flags may still change. Built for Claude Code's OAuth credential
files; the routing math is backend-agnostic.

## When to use it

You run an agent (or several) on Claude Code with more than one subscription and
want to:

- avoid hitting a 5-hour or weekly limit while another slot sits idle,
- spread load so weekly windows are used at a steady pace instead of burning
  one slot first,
- detect broken or drifted credentials (expired refresh token, a stray `/login`
  that overwrote the live file) before an unattended session fails on them.

If you only have one subscription, you don't need this package.

How it fits with the rest of gptme-contrib:

- [credential-slots](../credential-slots/README.md) — the low-level, offline
  slot-switching safety checks this package builds on (required dependency).
- [gptme-usage](../gptme-usage/README.md) — cross-backend cost and quota
  registry. Deliberately separate: `gptme-usage` never flips credentials.

## Install

`gptme-subscription` depends on `credential-slots`, which is not on PyPI, so
install both from the monorepo:

```bash
pip install \
  "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/credential-slots" \
  "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-subscription"
```

Or, from a gptme-contrib checkout (uv workspace):

```bash
uv sync --package gptme-subscription
```

## How it works

A slot is a named credential file next to the live symlink:

```text
~/.claude/.credentials.json                    # symlink → one of the slots
~/.claude/.credentials.json.alice
~/.claude/.credentials.json.alice.fingerprint.json   # identity baseline (optional)
~/.claude/.credentials.json.bob
```

On each evaluation the manager:

1. Reads live quota for the active slot from your **usage script** (5-hour
   window, weekly, weekly Sonnet — three independent limits).
2. Returns a decision — `stay` or `switch` — with a reason. Switch reasons
   cover exhaustion, forward-routing load to an idle slot, and rebalancing when
   the primary is ahead of its weekly pace.
3. With `--execute`, flips the symlink via `credential-slots`, refusing expired
   or unreadable slots and (optionally) deferring while an autonomous session
   holds a lock.
4. Logs switches and rebalance holds under the state directory.

## Quickstart

```bash
# Show active slot + symlink state (no network)
gptme-subscription --slots alice,bob --status

# Check credential expiry for every slot (file-based, no network)
gptme-subscription --slots alice,bob --check-auth

# Same, but actually probe each slot through your usage script
gptme-subscription --slots alice,bob \
  --usage-script ~/bin/check-claude-usage.sh \
  --check-auth --probe

# Evaluate quota and print the recommendation (preview only)
gptme-subscription --slots alice,bob --usage-script ~/bin/check-claude-usage.sh

# ...and apply it
gptme-subscription --slots alice,bob --usage-script ~/bin/check-claude-usage.sh --execute

# Force-switch (operator override; also records a hold so automation
# doesn't immediately switch back)
gptme-subscription --slots alice,bob --switch bob --execute
```

`--json` gives machine-readable output for evaluate, `--status`,
`--check-auth`, `--check-identity`, `--baseline-identity` and `--heal-drift`
(not `--switch` or `--reauth-instructions`). `--dry-run`/`-n` always wins over
`--execute`.

## CLI reference

Actions (the default with none of these is *evaluate*):

| Flag | What it does |
|---|---|
| `--status` | Print active slot, primary, available slots, symlink target |
| `--check-auth [--probe]` | Report credential expiry per slot; `--probe` makes a real call through the usage script. Exits 1 if any slot needs attention |
| `--reauth-instructions SLOT` | Print re-login steps for a slot |
| `--switch SLOT` | Force-switch (preview unless `--execute`). A slot with an expired access token is accepted if a probe confirms its refresh token works |
| `--check-identity` | Detect refresh-token fingerprint drift. Exits 0 clean, 1 drift, 2 no baseline |
| `--baseline-identity SLOT` | Capture the current fingerprint for a slot |
| `--heal-drift` | Repair a drifted live symlink (preview unless `--execute`) |
| `--execute` | Apply the recommended or forced change |

Config overrides: `--slots`, `--fallback-order`, `--primary`, `--creds-dir`,
`--state-dir`, `--usage-script`, `--lock-glob`, `--rate-limit-file`. Run
`gptme-subscription --help` for details.

## Configuration

CLI flags override environment variables, which override defaults.

| Setting | Env var | Default |
|---|---|---|
| Slot names | `GPTME_SUBSCRIPTION_SLOTS` | `primary` |
| Fallback order | `GPTME_SUBSCRIPTION_FALLBACK_ORDER` | all slots except primary |
| Credentials dir | `GPTME_SUBSCRIPTION_CREDS_DIR` | `~/.claude` |
| State dir | `GPTME_SUBSCRIPTION_STATE_DIR` | `$XDG_STATE_HOME/gptme-subscription` |
| Usage script | `GPTME_SUBSCRIPTION_USAGE_SCRIPT` | unset (no quota checks/probes) |
| Lock-file glob | `GPTME_SUBSCRIPTION_LOCK_GLOB` | unset (no session guard) |
| Weekly exhausted threshold | `GPTME_SUBSCRIPTION_WEEKLY_EXHAUSTED` | `0.85` |
| 5-hour exhausted threshold | `GPTME_SUBSCRIPTION_FIVE_HOUR_EXHAUSTED` | `0.90` |
| Weekly Sonnet exhausted threshold | `GPTME_SUBSCRIPTION_SONNET_WEEKLY_EXHAUSTED` | `0.95` |
| Primary probe cooldown | `GPTME_SUBSCRIPTION_PROBE_COOLDOWN` | `1800` (seconds) |

The primary slot is the first entry in the slot list unless `--primary` is
given. The lock glob points at PID lock files of long-running agent sessions;
while any referenced PID is alive, automated switches are deferred.

## Usage-script contract

Quota-driven modes need a script you provide. It is invoked as
`<script> --json` (plus `--no-cache` for probes and forced refreshes) against
the *live* credential and must print JSON shaped like:

```json
{
  "five_hour":        {"utilization": 0.42, "resets_in_seconds": 1234},
  "seven_day":        {"utilization": 0.71, "resets_in_seconds": 56789},
  "seven_day_sonnet": {"utilization": 0.88, "resets_in_seconds": 56789}
}
```

Utilization is a 0–1 fraction. A non-zero exit or unparsable output counts as
"could not check usage". Without a usage script the manager still works in the
read-only modes (`--status`, `--check-auth` without `--probe`,
`--check-identity`), but evaluation returns `could not check usage` and
`--probe` is skipped (reported as ok).

For `--check-auth --probe`, the script runs with `HOME` pointed at a temporary
directory whose `.claude/.credentials.json` is a symlink to the slot under test
(and `CLAUDE_HOME` set to that `.claude` dir), so it must read credentials from
`$HOME/.claude` or `$CLAUDE_HOME` rather than a hard-coded path.

## Re-authenticating a slot

Two failure modes look similar but need different responses:

1. **Access token past `expiresAt`** — common and harmless. Claude Code
   refreshes it from the long-lived refresh token on next use. `--check-auth`
   shows `[stale]` and "access token lapsed Nm ago — will refresh on next use".
   No action needed.
2. **Refresh token revoked / slot broken** — caught by `--check-auth --probe`
   (`probe=FAIL`), or a missing/malformed credential file. Re-login:

```bash
gptme-subscription --reauth-instructions alice   # prints these steps

claude   # then run /login                                          # 1. re-login
cp ~/.claude/.credentials.json ~/.claude/.credentials.json.alice    # 2. persist into the slot
ln -sf .credentials.json.alice ~/.claude/.credentials.json          # 3. restore the symlink (last)
gptme-subscription --slots alice,bob --baseline-identity alice      # 4. optional: re-baseline identity
```

Order matters: `/login` (and routine token refresh) *replaces* the live symlink
with a regular file, so a symlink created before `/login` is clobbered. Copy the
fresh tokens into the slot first, then restore the symlink.

## Python API

```python
from pathlib import Path
from gptme_subscription import Config, SubscriptionManager, check_credential_file

cfg = Config(
    subscriptions=["alice", "bob"],
    usage_script=Path.home() / "bin" / "check-claude-usage.sh",
)
sm = SubscriptionManager(cfg)

active = sm.get_active_subscription()
decision = sm.evaluate(sm.check_usage(), active)
print(decision.action, decision.target, decision.reason)
if decision.action == "switch" and decision.target:
    sm.switch_to(decision.target, decision.reason)

# Inspect a credential file without network access
info = check_credential_file(cfg.slot_path("alice"), "alice")
print(info.status, info.expires_in_seconds)
```

Pure-logic helpers (`subscription_pressure_from_usage`,
`capacity_aware_fallback_order`, `compute_window_pacing`,
`best_lower_pressure_fallback`, …) are exported for callers building their own
orchestration; see `gptme_subscription.__all__`.
