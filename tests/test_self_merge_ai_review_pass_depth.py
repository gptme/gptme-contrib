"""Pass depth is a confidence level: a late-pass-only finding does not block.

Measured on gptme/gptme findings posted since 2026-10-19 (n=650 distinct): the
retraction/rejection rate is 6.4% for pass-1 findings (40/622), 16.7% for pass 2
(7/42) and 20.5% for pass 3 (8/39). Pass depth was a confidence signal nothing
in the merge gate read, so a finding only one late pass ever raised could hold a
PR for a full review round on a false positive.

The rule the gate now applies, per finding fingerprint:

- pass 1 raised it -> hold (pass-1 findings are the ones worth a round);
- a late pass raised it AND a second pass or a second model corroborated it ->
  hold (that is independent evidence, not a sample);
- a late pass raised it alone -> advisory, does not block.

Every unreadable input fails closed: no provenance, no parseable pass number, or
no fingerprint keeps the previous behaviour where every P0/P1 blocks.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "github" / "self-merge-check.py"
)
spec = importlib.util.spec_from_file_location(
    "self_merge_check_pass_depth", MODULE_PATH
)
if spec is None or spec.loader is None:
    pytest.skip(f"Could not load {MODULE_PATH}", allow_module_level=True)
smc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smc
spec.loader.exec_module(smc)

AUTHOR = "TimeToBuildBob"
HEAD_SHA = "9af637526840aa11bb22cc33dd44ee55ff667788"
MARKER_SHA = HEAD_SHA[:12]
FP = "67396faad28b"

FULL_CONSENSUS = {
    "requested": 3,
    "answered": 3,
    "jobs_requested": 9,
    "jobs_answered": 9,
    "min_agreement": 2,
    "min_agreement_requested": 2,
    "rejected": 4,
    "failures": [],
}


def _marker(**overrides: Any) -> dict[str, Any]:
    marker: dict[str, Any] = {
        "sha": MARKER_SHA,
        "score": 3,
        "engine": "llm",
        "consensus": dict(FULL_CONSENSUS),
        "history": [],
        "suppressed": [],
        "findings": [{"fp": FP, "severity": "P1"}],
    }
    marker.update(overrides)
    return marker


def _finding_thread(severity: str, *, resolved: bool = False, total: int = 1) -> dict:
    body = (
        f"{smc._AI_REVIEW_FINDING_MARKER}\n"
        f'<!-- bob-ai-review-fp {{"fp": "{FP}"}} -->\n'
        f"🛑 **{severity}** — the hash ignores .state"
    )
    return {
        "isResolved": resolved,
        "comments": {
            "totalCount": total,
            "nodes": [{"author": {"login": AUTHOR}, "body": body}],
        },
    }


def _status(marker: dict[str, Any], threads: list[dict[str, Any]]) -> dict[str, Any]:
    """The gate's AI-review leg with no network: marker and threads injected."""
    result: dict[str, Any] = smc.fetch_ai_review_status(
        "o/r",
        1,
        head_sha=HEAD_SHA,
        expected_author=AUTHOR,
        marker_result=marker,
        review_data=([], threads),
    )
    return result


def test_pass_one_finding_still_blocks() -> None:
    """The baseline: a pass-1 P1 with an open thread is exactly what must hold."""
    marker = _marker(
        finding_provenance={FP: [{"model": "claude-code:sonnet", "pass": 1}]}
    )
    result = _status(marker, [_finding_thread("P1")])
    assert result["accepted"] is False
    assert result["refusal"] == "disposition_shortfall"


def test_late_pass_only_finding_is_advisory() -> None:
    """A finding only pass 2 ever reported does not block on its own."""
    marker = _marker(
        finding_provenance={FP: [{"model": "claude-code:sonnet", "pass": 2}]}
    )
    result = _status(marker, [_finding_thread("P1")])
    assert result["accepted"] is True, result
    assert "late-pass-only finding(s) advisory" in (result["detail"] or "")


def test_late_pass_corroborated_by_another_pass_blocks() -> None:
    """Two passes raising it is independent evidence, not a single sample."""
    marker = _marker(
        finding_provenance={
            FP: [
                {"model": "claude-code:sonnet", "pass": 2},
                {"model": "claude-code:sonnet", "pass": 3},
            ]
        }
    )
    result = _status(marker, [_finding_thread("P1")])
    assert result["accepted"] is False


def test_late_pass_corroborated_by_another_model_blocks() -> None:
    """A second reviewer raising the same fingerprint corroborates it."""
    marker = _marker(
        finding_provenance={
            FP: [
                {"model": "claude-code:sonnet", "pass": 2},
                {"model": "openrouter:deepseek/deepseek-v4-flash-0731", "pass": 2},
            ]
        }
    )
    result = _status(marker, [_finding_thread("P1")])
    assert result["accepted"] is False


def test_same_pass_and_model_duplicated_is_not_corroboration() -> None:
    """Two identical provenance entries are one observation, not two."""
    marker = _marker(
        finding_provenance={
            FP: [
                {"model": "claude-code:sonnet", "pass": 2},
                {"model": "claude-code:sonnet", "pass": 2},
            ]
        }
    )
    result = _status(marker, [_finding_thread("P1")])
    assert result["accepted"] is True


def test_absent_provenance_fails_closed() -> None:
    """Legacy markers carry no provenance block: keep blocking, as before."""
    result = _status(_marker(), [_finding_thread("P1")])
    assert result["accepted"] is False
    assert result["refusal"] == "disposition_shortfall"


def test_unreadable_pass_number_fails_closed() -> None:
    """A provenance entry whose pass is not an int must not buy an exemption."""
    marker = _marker(finding_provenance={FP: [{"model": "m", "pass": "2"}]})
    result = _status(marker, [_finding_thread("P1")])
    assert result["accepted"] is False


def test_malformed_provenance_container_fails_closed() -> None:
    """A provenance value that is not a list is unreadable, not empty."""
    marker = _marker(finding_provenance={FP: {"model": "m", "pass": 2}})
    result = _status(marker, [_finding_thread("P1")])
    assert result["accepted"] is False


def test_advisory_fp_is_reported_in_accept_detail() -> None:
    """The demotion stays visible where operators read the merge decision."""
    marker = _marker(
        finding_provenance={FP: [{"model": "m", "pass": 3}]},
    )
    assert smc._ai_review_advisory_fps(marker) == {FP}
    detail = _status(marker, [_finding_thread("P1")])["detail"] or ""
    assert "1 late-pass-only finding(s) advisory" in detail


def test_advisory_set_is_empty_without_provenance() -> None:
    assert smc._ai_review_advisory_fps(_marker()) == set()
    assert smc._ai_review_advisory_fps(None) == set()
