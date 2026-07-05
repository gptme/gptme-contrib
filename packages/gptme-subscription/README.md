# gptme-subscription

Quota-aware subscription/credential rotation for Claude Code (and other
OAuth-backed agent backends). Provides both a Python API and a
`gptme-subscription` CLI extracted from
[Bob's `manage-subscription.py`](https://github.com/ErikBjare/bob/blob/master/scripts/manage-subscription.py).

## Status

Alpha — extracted in [gptme/gptme-contrib#831](https://github.com/gptme/gptme-contrib/issues/831).

## What it does

Given multiple Claude Max subscription "slots" (each a
`~/.claude/.credentials.json.<name>` file plus a sibling fingerprint
sidecar), this package:

1. Reads live quota (5h window, weekly Opus, weekly Sonnet — three
   independent limits with different reset schedules).
2. Decides whether to **stay**, **switch**, **forward-route** to spread
   load, or **rebalance** when the primary slot is ahead of pace.
3. Flips the `~/.claude/.credentials.json` symlink atomically via
   [credential-slots](../credential-slots/), with optional autonomous-session
   lock guards so a running agent doesn't get its credentials swapped out
   mid-request.
4. Detects credential issues — expired access tokens, missing files,
   refresh-token drift after a stray `/login`.

## CLI quickstart

```bash
# Show active slot + symlink state
gptme-subscription --slots bob,alice,erik --status

# Check credential expiry for every slot (file-based, no network)
gptme-subscription --slots bob,alice,erik --check-auth

# Same, but actually probe each slot via a usage script
gptme-subscription --slots bob,alice,erik \
  --usage-script ~/bin/check-claude-usage.sh \
  --check-auth --probe

# Print re-auth steps for a specific slot
gptme-subscription --reauth-instructions alice

# Evaluate and apply quota-driven decision
gptme-subscription --slots bob,alice,erik \
  --usage-script ~/bin/check-claude-usage.sh \
  --execute

# Force-switch (operator override)
gptme-subscription --slots bob,alice,erik --switch alice --execute
```

Run `gptme-subscription --help` for the full flag list and the
environment-variable defaults.

## Configuration

| Setting | Env var | Default |
|---|---|---|
| Slot names | `GPTME_SUBSCRIPTION_SLOTS` | `["primary"]` |
| Fallback order | `GPTME_SUBSCRIPTION_FALLBACK_ORDER` | all slots except primary |
| Credentials dir | `GPTME_SUBSCRIPTION_CREDS_DIR` | `~/.claude` |
| State dir | `GPTME_SUBSCRIPTION_STATE_DIR` | `$XDG_STATE_HOME/gptme-subscription` |
| Usage script | `GPTME_SUBSCRIPTION_USAGE_SCRIPT` | unset (no probes) |
| Lock-file glob | `GPTME_SUBSCRIPTION_LOCK_GLOB` | unset (no guard) |
| Weekly exhausted | `GPTME_SUBSCRIPTION_WEEKLY_EXHAUSTED` | `0.85` |
| Probe cooldown | `GPTME_SUBSCRIPTION_PROBE_COOLDOWN` | `1800` (s) |

CLI flags override env vars; env vars override defaults.

## Re-authenticating an expired slot

Two distinct failure modes look similar but need different responses.

**1. Access token is past `expiresAt`** (very common, harmless):

The credential file contains an `expiresAt` timestamp on the *access
token*. Claude Code refreshes it automatically using the long-lived
refresh token. `--check-auth` will print `[stale]` and report "access
token lapsed Nm ago — will refresh on next use". **No action needed.**

**2. Refresh token is revoked / slot is broken:**

Detected by `--check-auth --probe` — the probe makes a real API call
through the slot, so if the refresh fails (revoked token, account
suspended, expired session) you get `probe=FAIL`. Also covers missing or
malformed credential files.

To re-auth:

```bash
# Print the steps for a specific slot
gptme-subscription --reauth-instructions alice
```

The flow, in short:

```bash
# 1. Make the broken slot the live credential
ln -sf .credentials.json.alice ~/.claude/.credentials.json

# 2. Re-login interactively
claude
> /login

# 3. Persist the refreshed tokens back into the slot file
cp ~/.claude/.credentials.json ~/.claude/.credentials.json.alice

# 4. (Optional) re-baseline identity drift detection
gptme-subscription --slots bob,alice,erik --baseline-identity alice
```

## Python API

```python
from pathlib import Path
from gptme_subscription import (
    Config,
    SubscriptionManager,
    check_credential_file,
)

cfg = Config(
    subscriptions=["bob", "alice", "erik"],
    fallback_order=["alice", "erik"],
    usage_script=Path("/home/me/bin/check-claude-usage.sh"),
)
sm = SubscriptionManager(cfg)

usage = sm.check_usage()
decision = sm.evaluate(usage, sm.get_active_subscription())
if decision.action == "switch" and decision.target:
    sm.switch_to(decision.target, decision.reason)

# Or just inspect credentials:
info = check_credential_file(cfg.slot_path("bob"), "bob")
print(info.status, info.expires_in_seconds)
```

The pure-logic helpers (`subscription_pressure_from_usage`,
`capacity_aware_fallback_order`, `compute_window_pacing`, …) are still
exported for callers that want to build their own orchestration.

## Usage-script contract

`--usage-script` expects a script that prints quota JSON when invoked
with `--json` (and `--no-cache` for a forced refresh). The expected shape
mirrors
[`scripts/check-claude-usage.sh`](https://github.com/ErikBjare/bob/blob/master/scripts/check-claude-usage.sh):

```json
{
  "five_hour":        {"utilization": 0.42, "resets_in_seconds": 1234},
  "seven_day":        {"utilization": 0.71, "resets_in_seconds": 56789},
  "seven_day_sonnet": {"utilization": 0.88, "resets_in_seconds": 56789},
  "_pacing": {"actual_utilization": 0.71, "target_utilization": 0.50, "status": "overusing"}
}
```

Without a usage script the manager still works in **read-only modes**
(`--status`, `--check-auth` without `--probe`, `--check-identity`), but
quota-driven evaluation returns `"could not check usage"`.
