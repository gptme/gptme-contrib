"""usage_summary balance_delta survives a key rotation (lifetime counter reset)."""

from __future__ import annotations

import json

from gptme_review.core import usage_summary


def _snapshots(tmp_path, used_values):
    path = tmp_path / "usage.jsonl"
    rows = [
        {
            "ts": f"2026-11-1{i}T10:00:00+00:00",
            "record_type": "balance_snapshot",
            "used": used,
        }
        for i, used in enumerate(used_values)
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def test_monotonic_counter_delta(tmp_path):
    assert usage_summary(_snapshots(tmp_path, [2.0, 2.4]))["balance_delta"] == 0.4


def test_counter_reset_keeps_post_rotation_spend(tmp_path):
    # Key rotated between the 2nd and 3rd snapshot: 243.6 -> 0 -> 12.0.
    summary = usage_summary(_snapshots(tmp_path, [240.0, 243.6, 0.0, 12.0]))
    assert summary["balance_delta"] == 15.6
