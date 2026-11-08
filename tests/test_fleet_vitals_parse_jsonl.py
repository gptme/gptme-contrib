"""parse_jsonl_lines must skip any unparseable line, not abort the report."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_vitals as fv  # noqa: E402

GOOD = json.dumps({"timestamp": "2026-11-07T12:00:00Z", "outcome": "noop"})


@pytest.mark.parametrize(
    "bad",
    [
        "{not json",
        # Valid JSON syntax, but int() refuses >4300 digits -> plain ValueError,
        # not JSONDecodeError.
        '{"timestamp": "2026-11-07T11:00:00Z", "output_tokens": ' + "9" * 5000 + "}",
        # Nesting deep enough to raise RecursionError inside json.loads.
        "[" * 200_000,
    ],
    ids=["malformed", "int-too-many-digits", "deep-nesting"],
)
def test_bad_line_is_skipped_and_good_line_kept(bad: str) -> None:
    out = fv.parse_jsonl_lines([bad, GOOD])
    assert len(out) == 1
    assert out[0][1]["outcome"] == "noop"
