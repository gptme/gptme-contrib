"""Tests for autoresearch self-diagnosis log parsing."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).parent.parent / "scripts" / "autoresearch" / "parse-diagnosis.py"
)
SPEC = importlib.util.spec_from_file_location("parse_diagnosis", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_parse_fields_prefers_last_tagged_values() -> None:
    text = """
Respond with ONLY these 3 lines, nothing else:
CAUSE: <infrastructure_bug|local_optimum|wrong_approach>
BUG_DETAIL: <file:function and specific symptom if infrastructure_bug, else NONE>
NEXT_FOCUS: <one concrete specific action the next iteration should take>

Bob:
CAUSE: infrastructure_bug
BUG_DETAIL: scripts/autoresearch/evals/bob-test-score.sh: scorer hard-caps at 0.999
NEXT_FOCUS: Change the score formula so the benchmark keeps headroom
"""

    parsed = MODULE.parse_fields(text)

    assert parsed == {
        "CAUSE": "infrastructure_bug",
        "BUG_DETAIL": "scripts/autoresearch/evals/bob-test-score.sh: scorer hard-caps at 0.999",
        "NEXT_FOCUS": "Change the score formula so the benchmark keeps headroom",
    }


def test_parse_fields_returns_defaults_when_tags_missing() -> None:
    parsed = MODULE.parse_fields("no structured answer here")

    assert parsed == {
        "CAUSE": "unknown",
        "BUG_DETAIL": "NONE",
        "NEXT_FOCUS": "",
    }
