"""Tests for autoresearch self-diagnosis log parsing."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).parent.parent / "scripts" / "autoresearch" / "parse-diagnosis.py"
)


@pytest.fixture(scope="session")
def parse_diagnosis():
    spec = importlib.util.spec_from_file_location("parse_diagnosis", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_fields_prefers_last_tagged_values(parse_diagnosis) -> None:
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

    parsed = parse_diagnosis.parse_fields(text)

    assert parsed == {
        "CAUSE": "infrastructure_bug",
        "BUG_DETAIL": "scripts/autoresearch/evals/bob-test-score.sh: scorer hard-caps at 0.999",
        "NEXT_FOCUS": "Change the score formula so the benchmark keeps headroom",
    }


def test_parse_fields_returns_defaults_when_tags_missing(parse_diagnosis) -> None:
    parsed = parse_diagnosis.parse_fields("no structured answer here")

    assert parsed == {
        "CAUSE": "unknown",
        "BUG_DETAIL": "NONE",
        "NEXT_FOCUS": "",
    }


def test_parse_fields_ignores_template_placeholders_when_field_is_omitted(
    parse_diagnosis,
) -> None:
    text = """
Respond with ONLY these 3 lines, nothing else:
CAUSE: <infrastructure_bug|local_optimum|wrong_approach>
BUG_DETAIL: <file:function and specific symptom if infrastructure_bug, else NONE>
NEXT_FOCUS: <one concrete specific action the next iteration should take>

Bob:
CAUSE: local_optimum
NEXT_FOCUS: Try a different implementation approach
"""

    parsed = parse_diagnosis.parse_fields(text)

    assert parsed == {
        "CAUSE": "local_optimum",
        "BUG_DETAIL": "NONE",
        "NEXT_FOCUS": "Try a different implementation approach",
    }
