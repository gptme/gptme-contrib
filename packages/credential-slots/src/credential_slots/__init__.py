"""Safe credential-slot rotation for agents running OAuth-backed subscriptions.

This package extracts the reusable pieces of Bob's ``manage-subscription.py``
so that other agents (alice, gordon, sven, ...) can inherit the same safety
guarantees without forking the logic.

Motivating incident (2026-04-23): Bob's live
``~/.claude/.credentials.json`` OAuth token became invalid server-side while
still claiming a future ``expiresAt``. Every autonomous session hit 401.
After three infra failures the crash-loop cooldown engaged and opus was
blocked for 1h. When an operator refreshed the token via ``/login`` it was
written to the live file only — a future ``--switch bob`` could silently
restore the stale credentials. See ``README.md`` for the full post-mortem.

Public API::

    from credential_slots import SlotManager

    mgr = SlotManager(
        creds_dir=Path.home() / ".claude",
        subscriptions=["bob", "alice"],
    )
    mgr.get_active_subscription()       # -> "bob" | None
    mgr.slot_is_fresh("bob")            # -> (True, "valid until ...")
    mgr.detect_live_slot_drift()        # -> dict | None
    mgr.switch_to("alice", "rebalance") # -> SwitchResult (result.ok, result.reason, result.deferred_locks)
    mgr.heal_drift_to("bob")            # -> SwitchResult (resync live → slot, restore symlink)
"""

from __future__ import annotations

from credential_slots.manager import (
    DEFAULT_GRACE_SECONDS,
    DEFAULT_LIVE_NAME,
    DEFAULT_SLOT_TEMPLATE,
    DriftInfo,
    SlotManager,
    SwitchResult,
    read_slot_expiry,
    slot_is_fresh,
)

__all__ = [
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_LIVE_NAME",
    "DEFAULT_SLOT_TEMPLATE",
    "DriftInfo",
    "SlotManager",
    "SwitchResult",
    "read_slot_expiry",
    "slot_is_fresh",
]

__version__ = "0.2.0"
