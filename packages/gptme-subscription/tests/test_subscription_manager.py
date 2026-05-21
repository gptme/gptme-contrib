"""Tests for gptme_subscription.manager."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

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
