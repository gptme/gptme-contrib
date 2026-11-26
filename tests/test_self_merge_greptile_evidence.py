"""Evaluation-time observations, not causal merge attribution or a new gate."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

PATH = Path(__file__).resolve().parents[1] / "scripts/github/self-merge-check.py"
spec = importlib.util.spec_from_file_location("self_merge_evidence", PATH)
assert spec and spec.loader
smc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smc
spec.loader.exec_module(smc)
HEAD = "a" * 40
OLD = "b" * 40
PACKET = {
    "comment_id": 123,
    "score": 4,
    "reviewed_commit": HEAD,
    "reviewed_at": "2026-11-26T12:00:00Z",
}


@pytest.fixture
def evaluate(monkeypatch):
    pr = {
        "author": {"login": "bob"},
        "title": "test",
        "files": [{"path": "tests/test_a.py"}],
        "headRefOid": HEAD,
        "baseRefName": "master",
        "labels": [],
        "state": "OPEN",
        "isDraft": False,
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
    }
    monkeypatch.setattr(smc, "fetch_pr", lambda *a: pr)
    monkeypatch.setattr(smc, "get_gh_user", lambda: "bob")
    monkeypatch.setattr(smc, "merge_permission", lambda *a: True)
    monkeypatch.setattr(smc, "_fetch_greptile_review_data", lambda *a: ([], []))
    monkeypatch.setattr(
        smc, "_fetch_ai_review_marker_checked", lambda *a, **kw: (None, False)
    )
    monkeypatch.setattr(
        smc,
        "fetch_greptile_status",
        lambda *a, **kw: {"has_review": True, "unresolved": 0},
    )
    monkeypatch.setattr(
        smc,
        "fetch_ai_review_status",
        lambda *a, **kw: {"accepted": True, "detail": "clean"},
    )
    monkeypatch.setattr(
        smc,
        "fetch_unresolved_human_threads",
        lambda *a, **kw: {"unresolved": 0, "total": 0, "authors": []},
    )
    monkeypatch.setattr(smc, "_fetch_contrib_pr_file_shapes", lambda *a: [])
    monkeypatch.setattr(
        smc, "merge_policy_tier_waiver", lambda *a, **kw: ([], None, None)
    )
    monkeypatch.setattr(smc, "run_gh", lambda *a, **kw: "")
    monkeypatch.setattr(smc, "run_gh_bytes", lambda *a, **kw: None)
    monkeypatch.setenv("SELF_MERGE_MIN_GREPTILE_SCORE", "4")
    return lambda: smc.evaluate_pr(
        "gptme/gptme-contrib", 999, workspace_repos=["gptme/gptme-contrib"]
    )


@pytest.mark.parametrize(
    "reviewed,carry,coverage,match",
    [
        (HEAD, True, "direct", True),
        (HEAD[:7], True, "direct", True),
        (OLD, True, "carried", False),
        (OLD, False, "not_covered", False),
        (None, False, "unknown", None),
        ("broken", False, "unknown", None),
    ],
)
def test_coherent_packet_and_existing_coverage(
    evaluate, monkeypatch, reviewed, carry, coverage, match
):
    packet = dict(PACKET, reviewed_commit=reviewed)
    read = Mock(side_effect=[packet, dict(PACKET, comment_id=999, score=1)])
    monkeypatch.setattr(smc, "_fetch_greptile_summary_signal", read)
    coverage_check = Mock(return_value=carry)
    monkeypatch.setattr(smc, "_review_carries_to_head", coverage_check)
    result = evaluate()
    evidence = result.greptile_evidence
    assert evidence == dict(
        PACKET,
        reviewed_commit=reviewed if reviewed != "broken" else None,
        head_match=match,
        coverage=coverage,
        trigger="unknown",
    )
    assert read.call_count == 1
    assert coverage_check.call_count == (reviewed is not None)
    assert not any("below floor" in reason for reason in result.reasons)
    assert any("floor satisfied" in warning for warning in result.warnings) == (
        reviewed is not None and carry
    )


@pytest.mark.parametrize(
    "packet",
    [
        {},
        {"comment_id": True, "score": False, "reviewed_commit": 12, "reviewed_at": []},
        {
            "comment_id": -1,
            "score": 6,
            "reviewed_commit": "xx",
            "reviewed_at": "not a date",
        },
        {
            "comment_id": [],
            "score": [],
            "reviewed_commit": [],
            "reviewed_at": {},
            "coverage": [],
        },
    ],
)
def test_missing_malformed_evidence_is_unknown(evaluate, monkeypatch, packet):
    monkeypatch.setattr(smc, "_fetch_greptile_summary_signal", lambda *a: packet)
    result = evaluate()
    assert result.greptile_evidence == {
        "comment_id": None,
        "score": None,
        "reviewed_commit": None,
        "reviewed_at": None,
        "head_match": None,
        "coverage": "unknown",
        "trigger": "unknown",
    }


def test_no_review_does_not_add_a_summary_read(evaluate, monkeypatch):
    monkeypatch.setattr(
        smc, "fetch_greptile_status", lambda *a, **kw: {"has_review": False}
    )
    read = Mock(side_effect=AssertionError("new lookup"))
    monkeypatch.setattr(smc, "_fetch_greptile_summary_signal", read)
    assert evaluate().greptile_evidence is None
    read.assert_not_called()


def test_low_score_preserves_existing_refusal(evaluate, monkeypatch):
    monkeypatch.setattr(
        smc, "_fetch_greptile_summary_signal", lambda *a: dict(PACKET, score=2)
    )
    result = evaluate()
    assert result.greptile_evidence["score"] == 2
    assert "Greptile score 2/5 below floor 4/5" in result.reasons
    assert not result.eligible


def test_legacy_monkeypatch_seams_do_not_fabricate_packet(evaluate, monkeypatch):
    monkeypatch.setattr(smc, "greptile_summary_reviewed_commit", lambda *a: HEAD)
    monkeypatch.setattr(smc, "greptile_summary_score", lambda *a: 4)
    read = Mock(side_effect=AssertionError("new lookup"))
    monkeypatch.setattr(smc, "_fetch_greptile_summary_signal", read)
    result = evaluate()
    assert result.greptile_evidence is None
    assert any("floor satisfied" in warning for warning in result.warnings)
    read.assert_not_called()


def test_production_fetches_once_and_resets_between_evaluations(evaluate, monkeypatch):
    run = Mock(
        side_effect=[
            Mock(stdout=json.dumps(PACKET)),
            Mock(stdout=json.dumps(dict(PACKET, comment_id=456))),
        ]
    )
    monkeypatch.setattr(smc.subprocess, "run", run)
    first, second = evaluate(), evaluate()
    assert first.greptile_evidence["comment_id"] == 123
    assert second.greptile_evidence["comment_id"] == 456
    assert run.call_count == 2


@pytest.mark.parametrize("reviewed,expected", [(HEAD, "direct"), (OLD, "unknown")])
def test_operator_refresh_replaces_whole_packet_without_borrowing_coverage(
    evaluate, monkeypatch, reviewed, expected
):
    read = Mock(
        side_effect=[
            PACKET,
            dict(PACKET, comment_id=456, score=2, reviewed_commit=reviewed),
        ]
    )
    monkeypatch.setattr(smc, "_fetch_greptile_summary_signal", read)
    monkeypatch.setattr(
        smc, "fetch_ai_review_status", lambda *a, **kw: {"accepted": False}
    )
    monkeypatch.setattr(smc, "pointer_only_gitlink_ai_review_waiver", lambda **kw: None)
    monkeypatch.setattr(
        smc, "operator_approval_ai_review_waiver", lambda **kw: "operator approved"
    )
    result = evaluate()
    assert result.greptile_evidence == dict(
        PACKET,
        comment_id=456,
        score=2,
        reviewed_commit=reviewed,
        head_match=reviewed == HEAD,
        coverage=expected,
        trigger="unknown",
    )
    assert "Greptile score 2/5 below floor 4/5" in result.reasons
    assert read.call_count == 2


def test_substitution_refresh_preserves_coherent_new_packet(evaluate, monkeypatch):
    read = Mock(
        side_effect=[PACKET, dict(PACKET, comment_id=456, score=2, reviewed_commit=OLD)]
    )
    monkeypatch.setattr(smc, "_fetch_greptile_summary_signal", read)
    monkeypatch.setattr(
        smc,
        "fetch_ai_review_status",
        lambda *a, **kw: {"accepted": False, "detail": "absent"},
    )
    monkeypatch.setattr(smc, "pointer_only_gitlink_ai_review_waiver", lambda **kw: None)
    monkeypatch.setattr(smc, "operator_approval_ai_review_waiver", lambda **kw: None)
    monkeypatch.setattr(smc, "greptile_satisfies_ai_review", lambda **kw: "substitute")
    monkeypatch.setattr(
        smc, "_review_carries_to_head", lambda repo, base, sha, head: sha == head
    )
    result = evaluate()
    assert result.greptile_evidence == dict(
        PACKET,
        comment_id=456,
        score=2,
        reviewed_commit=OLD,
        head_match=False,
        coverage="not_covered",
        trigger="unknown",
    )
    assert read.call_count == 2


def test_json_serialization_exposes_optional_packet(evaluate, monkeypatch):
    from dataclasses import asdict

    monkeypatch.setattr(smc, "_fetch_greptile_summary_signal", lambda *a: PACKET)
    assert (
        json.loads(json.dumps(asdict(evaluate())))["greptile_evidence"]["comment_id"]
        == 123
    )


@pytest.mark.parametrize("payload", ["[]", "null", "broken JSON"])
def test_malformed_subprocess_packet_stays_unknown(evaluate, monkeypatch, payload):
    run = Mock(return_value=Mock(stdout=payload))
    monkeypatch.setattr(smc.subprocess, "run", run)
    result = evaluate()
    assert result.greptile_evidence["coverage"] == "unknown"
    assert result.greptile_evidence["head_match"] is None
    assert result.greptile_evidence["score"] is None
    run.assert_called_once()


@pytest.mark.parametrize("score", [None, 0, 2, 4, 5, True])
def test_evidence_does_not_change_existing_decisions(evaluate, monkeypatch, score):
    from dataclasses import asdict

    monkeypatch.setattr(
        smc, "_fetch_greptile_summary_signal", lambda *a: dict(PACKET, score=score)
    )
    observed = asdict(evaluate())
    monkeypatch.setattr(smc, "greptile_summary_reviewed_commit", lambda *a: HEAD)
    # The legacy score wrapper converts bool to int; do not change that policy
    # while making the independent evidence projection reject bool metadata.
    monkeypatch.setattr(
        smc,
        "greptile_summary_score",
        lambda *a: int(score) if isinstance(score, int) else None,
    )
    legacy = asdict(evaluate())
    observed.pop("greptile_evidence")
    legacy.pop("greptile_evidence")
    assert observed == legacy
