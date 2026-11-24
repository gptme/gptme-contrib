"""check_usage's stale-cache fallback returns resets_in_seconds that are
relative to when the cache was written, not to now, so evaluate() acts on
countdowns that may already have elapsed."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def test_stale_cache_five_hour_window_that_already_reset_does_not_force_switch(
    tmp_path: Path, make_manager
) -> None:
    failing = tmp_path / "usage.sh"
    failing.write_text("#!/bin/sh\nexit 1\n")
    failing.chmod(0o755)
    sm = make_manager(usage_script=failing)
    for slot in ("alice", "erik"):
        sm.config.slot_path(slot).write_text("{}")  # fresh fallback credentials
    # Both fallbacks were used recently, so the healthy-primary forward-routing
    # chain stays idle and only the 5h-exhaustion check can cause a switch.
    recent = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    sm.config.switch_log.write_text(
        "".join(f"{recent} switched to {s} — test\n" for s in ("alice", "erik", "bob"))
    )

    # Snapshot taken 3h ago: 5h window at 95%, resetting 2.5h after the scrape,
    # i.e. it reset ~30 minutes ago. Weekly/Sonnet are healthy.
    cache = tmp_path / "claude-usage-bob.json"
    cache.write_text(
        json.dumps(
            {
                "_ok": True,
                "seven_day": {"utilization": 0.30, "resets_in_seconds": 4 * 24 * 3600},
                "five_hour": {
                    "utilization": 0.95,
                    "resets_in_seconds": int(2.5 * 3600),
                },
                "seven_day_sonnet": {
                    "utilization": 0.20,
                    "resets_in_seconds": 4 * 24 * 3600,
                },
            }
        )
    )
    old = time.time() - 3 * 3600
    os.utime(cache, (old, old))

    usage = sm.check_usage(stale_cache=cache)
    assert usage is not None and usage.get("_stale")

    # The live countdown is 0 (already reset); stale data must not be read as
    # "5h exhausted, resets in 150m (>2h)".
    decision = sm.evaluate(usage, "bob")
    assert decision.action == "stay", decision.reason


def test_stale_cache_weekly_window_that_already_reset_is_not_exhausted(
    tmp_path: Path, make_manager
) -> None:
    failing = tmp_path / "usage.sh"
    failing.write_text("#!/bin/sh\nexit 1\n")
    failing.chmod(0o755)
    sm = make_manager(usage_script=failing)
    # Snapshot taken 3h ago: weekly at 98%, resetting 1h after the scrape, so
    # it reset ~2h ago. Aging the countdown alone would leave 98% on a fresh
    # window.
    cache = tmp_path / "claude-usage-bob.json"
    cache.write_text(
        json.dumps(
            {
                "_ok": True,
                "seven_day": {"utilization": 0.98, "resets_in_seconds": 3600},
                "five_hour": {"utilization": 0.10, "resets_in_seconds": 3 * 3600 + 60},
            }
        )
    )
    old = time.time() - 3 * 3600
    os.utime(cache, (old, old))

    usage = sm.check_usage(stale_cache=cache)
    assert usage is not None and usage.get("_stale")
    assert usage["seven_day"]["resets_in_seconds"] == 0
    assert usage["seven_day"]["utilization"] == 0.0
    # A window that has not reset keeps its utilization.
    assert usage["five_hour"]["utilization"] == 0.10
