"""Rebalance-state persistence for subscription routing hold windows.

Keeps a lightweight JSON file so that the routing layer can remember an
active hold (rebalance, forward-routing, or capacity-rebalance) across
invocations without depending on a specific agent's file layout.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def load_rebalance_state(
    state_path: Path,
    now: datetime | None = None,
) -> dict[str, object] | None:
    """Load persisted rebalance hold state if still active.

    Returns None when no file exists, the hold has expired, or the file is
    corrupt. Callers can inject a specific path to avoid coupling to an
    agent's state directory layout.
    """
    if not state_path.exists():
        return None
    current_time = now or datetime.now(timezone.utc)
    try:
        payload = json.loads(state_path.read_text())
        if not isinstance(payload, dict):
            state_path.unlink(missing_ok=True)
            return None
        hold_until = datetime.fromisoformat(payload["hold_until"])
        if hold_until <= current_time:
            state_path.unlink(missing_ok=True)
            return None
        payload["hold_until"] = hold_until
        return payload
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        state_path.unlink(missing_ok=True)
        return None


def save_rebalance_state(
    decision: dict[str, object],
    state_path: Path,
) -> None:
    """Persist the hold window created by the current decision.

    Handles rebalance, forward-routing, and capacity-rebalance modes.
    Only persists when mode is a recognized routing mode.
    """
    if decision.get("mode") not in (
        "rebalance",
        "forward-routing",
        "capacity-rebalance",
    ):
        return
    hold_until = decision.get("hold_until")
    if not isinstance(hold_until, str):
        return
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "hold_until": hold_until,
        "mode": decision.get("mode"),
        "pace_overage": decision.get("pace_overage"),
        "target_subscription": decision.get("target"),
        "reason": decision.get("reason"),
    }
    state_path.write_text(json.dumps(payload, indent=2) + "\n")


def clear_rebalance_state(state_path: Path) -> None:
    """Remove persisted rebalance hold state."""
    state_path.unlink(missing_ok=True)
