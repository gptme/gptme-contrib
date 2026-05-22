"""Tests for gptme_subscription.manager."""

from __future__ import annotations

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
