"""The standalone Claude parser must agree with the shared pacing contract."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from gptme_subscription.routing import compute_window_pacing_snapshot

PARSER = Path(__file__).resolve().parents[3] / "scripts/check-claude-usage-parser.py"


@pytest.mark.parametrize("used_percent", [0, 30, 45, 90, 100])
def test_pacing_canonical_field_matches_subscription(used_percent: int) -> None:
    proc = subprocess.run(
        [sys.executable, str(PARSER), "--json"],
        input=f"Current week (all models)\n{used_percent}% used\nResets 9pm\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    weekly = data["seven_day"]
    expected = compute_window_pacing_snapshot(
        weekly["utilization"],
        weekly["resets_in_seconds"],
        7 * 24 * 3600,
        target_utilization=0.9,
    )
    assert expected is not None
    pacing = data["_pacing"]
    assert pacing["pace_gap_used_minus_target"] == round(expected.pace_gap, 3)
    assert pacing["status"] == expected.status
    # Existing consumers keep the old target-minus-used field during migration.
    assert pacing["pace_gap"] == -pacing["pace_gap_used_minus_target"]
