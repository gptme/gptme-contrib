"""Legacy fleet imports and the inference CLI work without a workspace install."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_legacy_imports_resolve_to_the_packaged_helpers():
    # Exclude site-packages, editable installs and inherited PYTHONPATH.
    code = """
import sys
sys.path.insert(0, sys.argv[1])
import fleet_vitals as legacy
from gptme_vitals import fleet
for name in (
    'CORE_SCHEMA', 'HEADLINE_SCHEMA', 'STATUS_ORDER', 'make_core',
    'make_headline', 'roll_up_status', 'utcnow_rfc3339', 'emit',
):
    assert getattr(legacy, name) is getattr(fleet, name), name
assert legacy.make_core('example', activity={'status': 'down'})['status'] == 'down'
"""
    subprocess.run(
        [sys.executable, "-I", "-S", "-c", code, str(SCRIPTS)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_standalone_inference_cli_still_runs(tmp_path):
    records = tmp_path / "records.jsonl"
    records.write_text("")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(SCRIPTS / "fleet_vitals.py"),
            "inference",
            "--records",
            str(records),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert not result.stderr
    assert json.loads(result.stdout)["status"] == "stale"
