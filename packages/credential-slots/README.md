# credential-slots

Safe credential-slot rotation for gptme agents running Claude Max or other
OAuth-backed subscriptions.

A **slot** is a named credential file next to a live symlink, e.g.:

```text
~/.claude/.credentials.json             # symlink → one of the slots below
~/.claude/.credentials.json.bob
~/.claude/.credentials.json.alice
~/.claude/.credentials.json.erik
```

This package handles the offline safety checks around flipping that symlink:

- Reading a slot's stored `expiresAt`
- Refusing to switch into an expired or unreadable slot
- Detecting "drift" where the live file no longer matches any named slot
  (typical after an operator runs `/login`)
- Deferring automated switches while busy-signals are active

Everything else — usage polling, switch logging, rebalance strategy — stays
in the calling agent, with hooks (`on_switch`, `lock_guard`, `logger`) for
injection.

## Motivating incident — 2026-04-23

Bob's live `~/.claude/.credentials.json` OAuth token became invalid
server-side while still claiming a future `expiresAt`. Every autonomous
Claude Code session hit 401. The per-backend crash counter tripped after
three infra failures and opus was locked out for 1 h.

When Erik refreshed the token via `claude /login`, the new credentials
were written to the live file only — none of the named slots were
updated. The next `manage-subscription.py --switch bob` would have
silently put the stale token back.

Bob's `manage-subscription.py` was hardened in commit `e9ea27097`
(ErikBjare/bob) with three defensive checks. This package lifts those
checks out of Bob's workspace so other agents inherit the same
guarantees:

1. **Target-slot expiry validation** in `switch_to` — refuses known-bad
   tokens even under `force=True`.
2. **`detect_live_slot_drift`** — warns when the live file hashes to
   nothing the caller recognizes.
3. **Lock-guard injection** — automated switches defer while the caller
   reports active busy-signals; `force=True` overrides.

## Install

Dev-time (workspace mode, recommended for agents checking out
gptme-contrib as a submodule):

```bash
uv pip install -e packages/credential-slots
```

## Usage

```python
from pathlib import Path
from credential_slots import SlotManager

mgr = SlotManager(
    creds_dir=Path.home() / ".claude",
    subscriptions=["bob", "alice", "erik"],
    # Optional: defer automated switches while a busy-signal is present.
    lock_guard=lambda: [p.stem for p in Path("/tmp").glob("agent-*.lock")],
    # Optional: persist a switch log.
    on_switch=lambda sub, reason: switch_log.write_text(
        f"{datetime.utcnow()} switched to {sub} — {reason}\n"
    ),
    logger=print,  # defaults to silent
)

# Introspection
mgr.get_active_subscription()       # "bob" | None
mgr.get_available_subscriptions()   # ["bob", "alice"]

# Safety checks
ok, reason = mgr.slot_is_fresh("bob")
drift = mgr.detect_live_slot_drift()
if drift and drift["drift"]:
    warn("live creds file matches no named slot — run /login then persist")

# Switching
result = mgr.switch_to("alice", reason="bob quota exhausted")
if not result.ok:
    print(f"could not switch: {result.reason}")
    if result.deferred_locks:
        print(f"deferred by: {result.deferred_locks}")

# Healing drift after CC OAuth refresh (live file replaced by a regular file
# with a fresh token; named slots stranded at the old token).
# Caller decides which sub was active before the refresh — typically by
# inspecting their own switch log.
last_active = read_my_switch_log()  # caller-owned
if last_active:
    result = mgr.heal_drift_to(last_active)
    if result.ok:
        print(result.reason)  # "healed: synced live → .credentials.json.bob, ..."
```

## Design

- **Offline-only**: this package never makes network calls. Server-side
  token invalidation (valid `expiresAt` but the API still returns 401)
  must be detected by the agent's API response classifier.
- **Dependency injection for paths**: nothing is hardcoded to
  `~/.claude` or any single-agent layout. Tests instantiate the class
  against `tmp_path`; agents pass whatever directory works for them.
- **No workspace dependencies**: switch logs, rate-limit files,
  usage-polling scripts, and rebalance state all stay in the caller.
  This package provides the callbacks those callers plug into.

## Tests

```bash
uv pip install -e packages/credential-slots
uv run pytest packages/credential-slots/tests/ -v
```

## Status

- **`v0.2.0`** — added `SlotManager.heal_drift_to(sub, *, force=False)`
  for OAuth-refresh recovery. Ported from Bob's `manage-subscription.py`
  auto-heal logic (commit `b59d54d72`, ErikBjare/bob#685). Handles the
  recurring case where CC writes a fresh OAuth token to the live file
  (turning the symlink into a regular file) and every named slot is
  stranded at the old token.
- `v0.1.0` — initial release, ported from `manage-subscription.py` in
  ErikBjare/bob, commit `e9ea27097`.
