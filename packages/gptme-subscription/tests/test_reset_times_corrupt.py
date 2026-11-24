"""SubscriptionManager.record_sub_reset_time never recovers from a corrupt
reset-times file: invalid/empty JSON silently drops every future observation,
and a non-dict JSON payload raises out of evaluate()."""

from __future__ import annotations

import json

USAGE = {
    "seven_day": {"utilization": 0.42, "resets_in_seconds": 3 * 24 * 3600},
    "five_hour": {"utilization": 0.10, "resets_in_seconds": 3 * 3600},
    "seven_day_sonnet": {"utilization": 0.20, "resets_in_seconds": 3 * 24 * 3600},
}


def test_empty_reset_times_file_does_not_block_recording(make_manager) -> None:
    sm = make_manager()
    sm.config.reset_times_file.write_text("")  # e.g. truncated / touched file
    sm.evaluate(USAGE, "bob")
    sm.evaluate(USAGE, "bob")
    data = json.loads(sm.config.reset_times_file.read_text() or "{}")
    assert "bob" in data, "observation silently dropped; file never self-heals"


def test_truncated_reset_times_file_does_not_block_recording(make_manager) -> None:
    sm = make_manager()
    sm.config.reset_times_file.write_text('{"alice": {"observed_at": "2026-')
    sm.record_sub_reset_time("bob", 3600, USAGE)
    try:
        data = json.loads(sm.config.reset_times_file.read_text())
    except json.JSONDecodeError:
        data = {}
    assert "bob" in data, "observation silently dropped; file never self-heals"


def test_non_dict_reset_times_file_does_not_crash_evaluate(make_manager) -> None:
    sm = make_manager()
    sm.config.reset_times_file.write_text("[]\n")
    decision = sm.evaluate(USAGE, "bob")  # raises TypeError on current code
    assert decision.action in ("stay", "switch")
