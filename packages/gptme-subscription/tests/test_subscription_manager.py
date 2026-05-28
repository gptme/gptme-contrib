"""Tests for gptme_subscription.manager."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from gptme_subscription.config import Config
from gptme_subscription.manager import SubscriptionManager


def _make_manager(
    tmp_path: Path, *, usage_script: Path | None = None
) -> SubscriptionManager:
    creds_dir = tmp_path / "creds"
    creds_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config = Config(
        subscriptions=["bob", "alice", "erik"],
        primary="bob",
        fallback_order=["alice", "erik"],
        creds_dir=creds_dir,
        state_dir=state_dir,
        usage_script=usage_script,
    )
    return SubscriptionManager(config)


def test_seconds_since_last_primary_departure_tracks_primary_exit(
    tmp_path: Path,
) -> None:
    sm = _make_manager(tmp_path)
    now = datetime.now(timezone.utc)
    entries = [
        (now - timedelta(hours=8), "bob", "initial state"),
        (now - timedelta(hours=6), "alice", "weekly exhausted"),
        (now - timedelta(hours=1), "erik", "capacity rebalance"),
    ]
    sm.config.switch_log.write_text(
        "".join(
            f"{ts.strftime('%Y-%m-%dT%H:%M:%SZ')} switched to {sub} -- {reason}\n"
            for ts, sub, reason in entries
        )
    )

    seconds = sm.seconds_since_last_primary_departure()

    assert seconds is not None
    assert 6 * 3600 - 5 <= seconds <= 6 * 3600 + 5


def test_check_usage_returns_none_for_non_executable_script(tmp_path: Path) -> None:
    usage_script = tmp_path / "check-usage.sh"
    usage_script.write_text("#!/bin/sh\necho '{}'\n")
    usage_script.chmod(0o644)
    sm = _make_manager(tmp_path, usage_script=usage_script)

    assert sm.check_usage() is None


def test_detect_external_switch_ignores_unreadable_log(tmp_path: Path) -> None:
    sm = _make_manager(tmp_path)
    sm.config.switch_log.mkdir()
    sm.get_active_subscription = lambda: "alice"  # type: ignore[method-assign]

    sm.detect_external_switch()


# ---- slot_credential_is_stale ----


def test_slot_credential_is_stale_missing_file(tmp_path: Path) -> None:
    sm = _make_manager(tmp_path)
    # No credential file written → missing
    stale, msg = sm.slot_credential_is_stale("alice")
    assert stale
    assert "missing" in msg


def test_slot_credential_is_stale_old_file(tmp_path: Path) -> None:
    sm = _make_manager(tmp_path)
    cred = sm.config.slot_path("alice")
    cred.write_text("{}")
    # Backdate mtime to 10 days ago
    import os

    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
    os.utime(cred, (old_ts, old_ts))
    now = datetime.now(timezone.utc)
    stale, msg = sm.slot_credential_is_stale("alice", now=now)
    assert stale
    assert "10." in msg or "d old" in msg


def test_slot_credential_is_stale_fresh_file(tmp_path: Path) -> None:
    sm = _make_manager(tmp_path)
    cred = sm.config.slot_path("alice")
    cred.write_text("{}")
    # File just written → fresh
    now = datetime.now(timezone.utc)
    stale, msg = sm.slot_credential_is_stale("alice", now=now)
    assert not stale
    assert "d old" in msg


# ---- evaluate() stale-slot filtering ----


def _exhausted_usage() -> dict:
    return {
        "seven_day": {"utilization": 0.95, "resets_in_seconds": 3 * 24 * 3600},
        "five_hour": {"utilization": 0.95, "resets_in_seconds": 3 * 3600},
        "seven_day_sonnet": {"utilization": 0.20},
    }


def _write_cred(sm: SubscriptionManager, slot: str, *, age_days: float) -> None:
    """Write a slot credential file with a backdated mtime."""
    import os

    path = sm.config.slot_path(slot)
    path.write_text("{}")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=age_days)).timestamp()
    os.utime(path, (old_ts, old_ts))


def test_evaluate_records_observation_for_active_primary(tmp_path: Path) -> None:
    """``evaluate`` must record the live observation for the active slot even
    when it is the primary. Previously gated on ``active != primary``, which
    silently left the primary's weekly_utilization out of reset-times.json and
    broke downstream readers (Bob's vitals subscription pacing, the
    subscription-usage-history dashboard panel) for the slot that actually
    matters most.
    """
    sm = _make_manager(tmp_path)
    usage = {
        "seven_day": {"utilization": 0.42, "resets_in_seconds": 3 * 24 * 3600},
        "five_hour": {"utilization": 0.10, "resets_in_seconds": 3 * 3600},
        "seven_day_sonnet": {
            "utilization": 0.30,
            "resets_in_seconds": 3 * 24 * 3600,
        },
    }

    sm.evaluate(usage, "bob")

    reset_times = json.loads(sm.config.reset_times_file.read_text())
    assert "bob" in reset_times, "primary slot observation was not recorded"
    entry = reset_times["bob"]
    assert entry["weekly_utilization"] == pytest.approx(0.42)
    assert entry["five_hour_utilization"] == pytest.approx(0.10)
    assert entry["sonnet_weekly_utilization"] == pytest.approx(0.30)


def test_evaluate_skips_stale_fallback_picks_fresh(tmp_path: Path) -> None:
    sm = _make_manager(tmp_path)
    # alice is stale (17 days), erik is fresh
    _write_cred(sm, "alice", age_days=17)
    _write_cred(sm, "erik", age_days=1)
    usage = _exhausted_usage()
    now = datetime.now(timezone.utc)

    decision = sm.evaluate(usage, "bob", now=now)

    assert decision.action == "switch"
    assert decision.target == "erik"


def test_evaluate_stale_urgency_top_pick_reports_staleness_not_pressure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # P1: urgency reorders to put erik first, but erik is stale → alice is picked.
    # The reason should say "erik stale", not "(lower pressure / expiry-aware)".
    sm = _make_manager(tmp_path)
    monkeypatch.setattr(
        sm, "capacity_aware_fallback_order", lambda now=None: ["erik", "alice"]
    )
    _write_cred(sm, "alice", age_days=1)
    # erik has no cred file → missing → stale
    usage = _exhausted_usage()
    now = datetime.now(timezone.utc)

    decision = sm.evaluate(usage, "bob", now=now)

    assert decision.action == "switch"
    assert decision.target == "alice"
    assert "erik" in decision.reason
    assert "stale" in decision.reason
    assert "lower pressure" not in decision.reason


def test_evaluate_all_fallbacks_stale_stays(tmp_path: Path) -> None:
    sm = _make_manager(tmp_path)
    # Both fallbacks are stale (no file written → missing)
    usage = _exhausted_usage()
    now = datetime.now(timezone.utc)

    decision = sm.evaluate(usage, "bob", now=now)

    assert decision.action == "stay"
    assert "all fallbacks stale" in decision.reason
    assert "reauth needed" in decision.reason


def test_evaluate_fresh_fallback_selected_normally(tmp_path: Path) -> None:
    sm = _make_manager(tmp_path)
    # Both fallbacks are fresh
    _write_cred(sm, "alice", age_days=1)
    _write_cred(sm, "erik", age_days=2)
    usage = _exhausted_usage()
    now = datetime.now(timezone.utc)

    decision = sm.evaluate(usage, "bob", now=now)

    assert decision.action == "switch"
    assert decision.target in ("alice", "erik")


def _rebalancing_usage() -> dict:
    """Usage that is healthy but overusing pace — triggers the rebalance path."""
    return {
        "seven_day": {"utilization": 0.75, "resets_in_seconds": 0},
        "five_hour": {"utilization": 0.50, "resets_in_seconds": 3 * 3600},
        "seven_day_sonnet": {"utilization": 0.20},
        "_pacing": {
            "actual_utilization": 0.75,
            "target_utilization": 0.60,
            "status": "overusing",
        },
    }


def test_evaluate_rebalance_skipped_annotates_reason_when_all_fallbacks_stale(
    tmp_path: Path,
) -> None:
    sm = _make_manager(tmp_path)
    # No credential files written → all fallbacks stale (missing)
    usage = _rebalancing_usage()
    now = datetime.now(timezone.utc)

    decision = sm.evaluate(usage, "bob", now=now)

    assert decision.action == "stay"
    assert "rebalance skipped" in decision.reason
    assert "all fallbacks stale" in decision.reason


def _healthy_usage() -> dict:
    """Healthy usage, ~43% of the weekly period elapsed (would trigger
    proactive forward-routing absent a hold)."""
    return {
        "seven_day": {"utilization": 0.10, "resets_in_seconds": 4 * 24 * 3600},
        "five_hour": {"utilization": 0.05, "resets_in_seconds": 3600},
        "seven_day_sonnet": {"utilization": 0.05, "resets_in_seconds": 4 * 24 * 3600},
    }


def _manual_hold(now: datetime, target: str = "alice") -> dict:
    return {
        "active": target,
        "action": "stay",
        "target": target,
        "reason": f"manual switch via --switch {target}",
        "mode": "manual-switch",
        "hold_until": now + timedelta(hours=8),
        "hold_seconds": 8 * 3600,
    }


def test_manual_switch_hold_blocks_routing_on_fallback(tmp_path: Path) -> None:
    # On a fallback (alice), a manual-switch hold must keep us there instead of
    # forward-routing away the way the unprotected path used to.
    sm = _make_manager(tmp_path)
    now = datetime.now(timezone.utc)
    decision = sm.evaluate(
        _healthy_usage(), "alice", now=now, rebalance_state=_manual_hold(now)
    )

    assert decision.action == "stay"
    assert decision.mode == "manual-switch-hold"
    assert "manual-switch hold active" in decision.reason


def test_manual_switch_hold_respected_on_healthy_primary(tmp_path: Path) -> None:
    # An operator who manually switches back to the primary should also be held,
    # not proactively rebalanced/forward-routed off it.
    sm = _make_manager(tmp_path)
    now = datetime.now(timezone.utc)
    decision = sm.evaluate(
        _healthy_usage(),
        "bob",
        now=now,
        rebalance_state=_manual_hold(now, target="bob"),
    )

    assert decision.action == "stay"
    assert decision.mode == "manual-switch-hold"


def test_manual_switch_hold_does_not_strand_on_exhausted_primary(
    tmp_path: Path,
) -> None:
    # A manual hold must never strand us on an exhausted slot — exhaustion
    # fallback takes precedence over the hold.
    sm = _make_manager(tmp_path)
    _write_cred(sm, "alice", age_days=1)
    _write_cred(sm, "erik", age_days=1)
    now = datetime.now(timezone.utc)
    decision = sm.evaluate(
        _exhausted_usage(),
        "bob",
        now=now,
        rebalance_state=_manual_hold(now, target="bob"),
    )

    assert decision.action == "switch"
    assert decision.mode != "manual-switch-hold"
    assert decision.target in ("alice", "erik")


def test_manual_switch_hold_does_not_strand_on_exhausted_fallback(
    tmp_path: Path,
) -> None:
    # Safety: a manual hold on a fallback that is (or becomes) exhausted must not
    # trap us there — the `not blocked` guard lets failover proceed.
    sm = _make_manager(tmp_path)
    now = datetime.now(timezone.utc)
    decision = sm.evaluate(
        _exhausted_usage(),
        "alice",
        now=now,
        rebalance_state=_manual_hold(now, target="alice"),
    )

    assert decision.action == "switch"
    assert decision.mode != "manual-switch-hold"


def test_manual_switch_hold_does_not_block_wrong_slot(tmp_path: Path) -> None:
    """A hold targeting slot A must not block routing when active is slot B."""
    sm = _make_manager(tmp_path)
    _write_cred(sm, "alice", age_days=1)
    _write_cred(sm, "erik", age_days=1)
    now = datetime.now(timezone.utc)
    decision = sm.evaluate(
        _healthy_usage(),
        "bob",
        now=now,
        rebalance_state=_manual_hold(now, target="alice"),
    )

    # The hold is for alice, but bob is active — it should not block routing.
    assert decision.action == "switch"
    assert decision.mode != "manual-switch-hold"
    assert decision.target in ("alice", "erik")


def test_manual_switch_hold_does_not_block_wrong_slot_fallback(
    tmp_path: Path,
) -> None:
    """A hold targeting slot A must not block routing when active is slot B
    (fallback lane)."""
    sm = _make_manager(tmp_path)
    _write_cred(sm, "erik", age_days=1)
    now = datetime.now(timezone.utc)
    decision = sm.evaluate(
        _healthy_usage(),
        "alice",
        now=now,
        rebalance_state=_manual_hold(now, target="bob"),
    )

    # The hold is for bob, but alice is the active fallback.
    assert decision.action == "switch"
    assert decision.mode != "manual-switch-hold"


def test_record_manual_switch_hold_persists_state(tmp_path: Path) -> None:
    sm = _make_manager(tmp_path)
    now = datetime.now(timezone.utc)
    sm.record_manual_switch_hold("alice", now=now)

    payload = json.loads(sm.config.rebalance_state_file.read_text())
    assert payload["mode"] == "manual-switch"
    assert payload["target"] == "alice"
    assert payload["hold_seconds"] == sm.config.forward_routing_hold_seconds
    assert datetime.fromisoformat(payload["hold_until"]) > now

    # The persisted hold is honored by a subsequent evaluate() on the fallback.
    loaded = sm.load_rebalance_state(now=now)
    assert loaded is not None
    decision = sm.evaluate(_healthy_usage(), "alice", now=now, rebalance_state=loaded)
    assert decision.action == "stay"
    assert decision.mode == "manual-switch-hold"
