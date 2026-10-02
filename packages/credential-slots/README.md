# credential-slots

Safe rotation between multiple OAuth credential files ("slots") behind one
live symlink — for agents that switch between several Claude Max / OAuth-backed
subscriptions without landing on an expired or stale token.

**Status:** beta (`0.3.0`). Small, offline, no dependencies; used in production
as the switching layer under [gptme-subscription](../gptme-subscription/README.md).

## What it does

A **slot** is a named credential file next to a live symlink, e.g.:

```text
~/.claude/.credentials.json             # symlink → one of the slots below
~/.claude/.credentials.json.work
~/.claude/.credentials.json.personal
```

This package handles the offline safety checks around flipping that symlink:

- Reading a slot's stored `expiresAt` and refusing to switch into an expired,
  missing or unreadable slot — even with `force=True`.
- Detecting **drift**: the live file no longer matches any named slot
  (typical after running `/login`, which writes a fresh token to the live file
  and leaves the named slots stale). Switching back would silently restore the
  stale token.
- Detecting **identity drift**: a new login written *through* the symlink
  replaces a slot's identity while the files still match. Caught by comparing
  a stored refresh-token fingerprint (a sidecar file) with the current one.
- **Healing** drift by syncing the live file back into a slot and restoring
  the symlink.
- Deferring automated switches while the caller reports busy-signals (e.g.
  running sessions).

Everything else — usage polling, switch logging, when to rebalance — stays in
the caller, which plugs in through callbacks (`lock_guard`, `on_switch`,
`logger`). For quota-aware decisions on top of this, use
[gptme-subscription](../gptme-subscription/README.md).

## Install

```bash
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/credential-slots"
# or, from a gptme-contrib checkout:
uv pip install -e packages/credential-slots
```

## Usage

```python
from pathlib import Path
from credential_slots import SlotManager, reason_is_refreshable

mgr = SlotManager(
    creds_dir=Path.home() / ".claude",
    subscriptions=["work", "personal"],
    # Optional: names of holds that should defer automated switches.
    lock_guard=lambda: [p.stem for p in Path("/tmp").glob("agent-*.lock")],
    # Optional: persist a switch log.
    on_switch=lambda sub, reason: print(f"switched to {sub}: {reason}"),
    logger=print,  # defaults to silent
)

mgr.get_active_subscription()       # "work" | None
mgr.get_available_subscriptions()   # slots whose files exist

ok, reason = mgr.slot_is_fresh("personal")

# Switch (deferred while lock_guard returns holds, unless force=True)
result = mgr.switch_to("personal", reason="work quota exhausted")
if not result.ok:
    print(result.reason, result.deferred_locks)

# Live file matches no slot (e.g. after /login)?
drift = mgr.detect_live_slot_drift()
if drift and drift["drift"]:
    # The caller decides which slot was active before the refresh.
    print(mgr.heal_drift_to("work").reason)

# Slot identity replaced through the symlink?
ident = mgr.detect_slot_identity_drift("work")
if ident["drift"]:
    print(ident["reason"])
```

### Expired access token, valid refresh token

An expired access token is often still recoverable: Claude Code refreshes it
from the stored refresh token on first use. Use `reason_is_refreshable()` to
classify the `slot_is_fresh` reason (don't substring-match it — the grace-window
reason says "expires", not "expired"). If an online probe you ran confirms the
slot works, pass `probe_ok=True`:

```python
ok, reason = mgr.slot_is_fresh("work")
if not ok and reason_is_refreshable(reason) and my_online_probe("work"):
    mgr.switch_to("work", "probe confirmed", probe_ok=True)
```

`probe_ok` is deliberately narrow: it is honored only for expiry-class failures
on a slot that actually holds a refresh token. Missing or malformed slots are
still refused, and every bypass is logged.

## API

| Name | Purpose |
|------|---------|
| `SlotManager(creds_dir, subscriptions, *, slot_template, live_name, fingerprint_template, grace_seconds, lock_guard, on_switch, logger)` | Manages one family of slots |
| `.get_active_subscription()` / `.get_available_subscriptions()` | Introspection |
| `.slot_is_fresh(sub)` / `.read_slot_expiry(sub)` / `.slot_has_refresh_token(sub)` | Per-slot checks |
| `.switch_to(sub, reason, *, force=False, probe_ok=False)` | Flip the live symlink; returns `SwitchResult(ok, reason, deferred_locks)` |
| `.detect_live_slot_drift()` | `DriftInfo` (`drift`, `matching_slot`, `live_hash`, `slot_hashes`) or `None` |
| `.heal_drift_to(sub, *, force=False)` | Sync the drifted live file into `sub`'s slot and re-symlink |
| `.detect_slot_identity_drift(sub)` / `.capture_slot_fingerprint(sub)` | Refresh-token fingerprint checks (`switch_to` and `heal_drift_to` capture automatically) |
| `reason_is_refreshable(reason)` | Whether a freshness failure is only an expiry problem |
| `read_slot_expiry(path)`, `slot_is_fresh(path, ...)`, `compute_slot_fingerprint(path)` | Path-based helpers |

Defaults: live file `.credentials.json`, slots `.credentials.json.{sub}`,
fingerprints `.credentials.json.{sub}.fingerprint.json`, grace window 300 s.

## Design

- **Offline-only.** No network calls. Server-side invalidation (a valid-looking
  `expiresAt` while the API returns 401) must be detected by the caller.
- **Paths are injected.** Nothing is hardcoded to `~/.claude`; tests run
  against a temp directory.
- **No workspace state.** Switch logs, rate-limit files and rebalance state
  belong to the caller.

Why it exists: a live OAuth token was invalidated server-side while still
claiming a future `expiresAt`, and the operator's `/login` fix wrote the new
token only to the live file — so the next scripted switch would have silently
restored the stale one. These checks make that class of mistake fail loudly.

## Tests

```bash
uv run pytest packages/credential-slots/tests/ -v
```

## Changelog

- Unreleased — `switch_to(..., probe_ok=True)` and `reason_is_refreshable()`
  for expired-access-token slots with a confirmed refresh.
- `0.3.0` — identity-drift detection via refresh-token fingerprints.
- `0.2.0` — `SlotManager.heal_drift_to()` for OAuth-refresh recovery.
- `0.1.0` — initial release.
