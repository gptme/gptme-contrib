"""On a fallback slot, the forward-routing chain picks the next fallback without
the stale-credential filter that every other switch path applies (exhaustion,
rebalance, primary forward-routing). A stale sibling is chosen every run; the
switch is refused, and evaluate never reaches the 'probe primary' fallthrough."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gptme_subscription.config import Config
from gptme_subscription.manager import SubscriptionManager


def _manager(tmp_path: Path) -> SubscriptionManager:
    creds = tmp_path / "creds"
    creds.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    return SubscriptionManager(
        Config(
            subscriptions=["bob", "alice", "erik"],
            primary="bob",
            fallback_order=["alice", "erik"],
            creds_dir=creds,
            state_dir=state,
            usage_script=None,
        )
    )


def _write(sm: SubscriptionManager, slot: str, age_days: float) -> None:
    p = sm.config.slot_path(slot)
    p.write_text("{}")
    ts = (datetime.now(timezone.utc) - timedelta(days=age_days)).timestamp()
    os.utime(p, (ts, ts))


def test_fallback_forward_routing_skips_stale_slot(tmp_path: Path) -> None:
    sm = _manager(tmp_path)
    _write(sm, "bob", 0.1)
    _write(sm, "alice", 0.1)
    _write(sm, "erik", 30)  # stale (>7d)
    assert sm.slot_credential_is_stale("erik")[0]

    usage = {
        "seven_day": {"utilization": 0.10, "resets_in_seconds": 4 * 24 * 3600},
        "five_hour": {"utilization": 0.05, "resets_in_seconds": 3600},
        "seven_day_sonnet": {"utilization": 0.05, "resets_in_seconds": 4 * 24 * 3600},
    }
    d = sm.evaluate(usage, "alice", now=datetime.now(timezone.utc))
    assert d.target != "erik", f"routed to stale slot erik: {d.reason}"
