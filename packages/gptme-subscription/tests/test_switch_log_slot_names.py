"""Switch-log parsing matches slot names by substring / ``\\w+``, so slot
names that are prefixes of each other or contain '-' are misattributed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _write_log(sm, entries) -> None:
    now = datetime.now(timezone.utc)
    sm.config.switch_log.write_text(
        "".join(
            f"{(now - timedelta(hours=h)).strftime('%Y-%m-%dT%H:%M:%SZ')} "
            f"switched to {sub} — {reason}\n"
            for h, sub, reason in entries
        )
    )


def test_seconds_since_last_switch_to_does_not_match_prefix_slot(make_manager) -> None:
    sm = make_manager(subscriptions=["main", "slot-1", "slot-10"])
    _write_log(sm, [(48, "slot-1", "x"), (1, "slot-10", "y"), (0.5, "main", "z")])
    seconds = sm.seconds_since_last_switch_to("slot-1")
    assert seconds is not None
    # slot-1 was last used 48h ago, not 1h ago (that was slot-10)
    assert seconds > 40 * 3600, f"got {seconds}s: matched 'switched to slot-10'"


def test_detect_external_switch_with_prefix_slot(make_manager, monkeypatch) -> None:
    sm = make_manager(subscriptions=["slot-1", "slot-10"])
    _write_log(sm, [(1, "slot-10", "y")])
    monkeypatch.setattr(sm, "get_active_subscription", lambda: "slot-1")
    sm.detect_external_switch()
    last = sm.config.switch_log.read_text().splitlines()[-1]
    assert (
        "switched to slot-1 —" in last
    ), f"flip slot-10 -> slot-1 not logged: {last!r}"


def test_primary_departure_cooldown_with_hyphenated_slots(make_manager) -> None:
    sm = make_manager(subscriptions=["work-main", "work-alt"])
    _write_log(sm, [(8, "work-main", "init"), (0.1, "work-alt", "weekly exhausted")])
    seconds = sm.seconds_since_last_primary_departure()
    # Departed primary 6 minutes ago -> cooldown must see it.
    assert seconds is not None and seconds < 3600


def test_infer_active_slot_from_log_with_hyphenated_slots(make_manager) -> None:
    sm = make_manager(subscriptions=["work-main", "work-alt"])
    _write_log(sm, [(8, "work-main", "init"), (1, "work-alt", "x")])
    assert sm.infer_active_slot_from_log() == "work-alt"
