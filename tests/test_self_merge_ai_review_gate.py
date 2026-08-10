"""Our own review may satisfy the self-merge gate — but only at full strength.

The gate hard-required Greptile, so on a repo Greptile does not cover it was
unsatisfiable: gptme/gptme-contrib#1382 reported

    Eligible: NO
    - Greptile review not found

while our self-hosted reviewer had already posted a complete review of that PR.

Strictness is set by which direction the errors run. The reviewer's measured
precision is 37%, but precision governs *false findings*, and a false finding
lowers the score, which makes the gate more conservative — a wasted round, not a
bad merge. The dangerous direction is recall: a missed bug yields a clean score
and an unreviewed merge. So the tests below are mostly about refusing to accept a
5/5 that was reached on thin evidence — stale head, degraded consensus, abstain,
forged author — and about the Greptile path being untouched.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "github" / "self-merge-check.py"
)
spec = importlib.util.spec_from_file_location("self_merge_check_ai_gate", MODULE_PATH)
if spec is None or spec.loader is None:
    pytest.skip(f"Could not load {MODULE_PATH}", allow_module_level=True)
smc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smc
spec.loader.exec_module(smc)

AUTHOR = "TimeToBuildBob"
HEAD_SHA = "9af637526840aa11bb22cc33dd44ee55ff667788"
MARKER_SHA = HEAD_SHA[:12]

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
    """A clean, current, full-consensus marker as the reviewer writes it."""
    marker: dict[str, Any] = {
        "sha": MARKER_SHA,
        "score": 5,
        "engine": "llm",
        "consensus": dict(FULL_CONSENSUS),
        "history": [],
        "suppressed": [],
    }
    marker.update(overrides)
    return marker


def _comment(marker: dict[str, Any] | None, author: str = AUTHOR) -> str:
    """One `gh api --jq` output line: a comment carrying the summary marker."""
    body = "## AI Review\n\nConfidence Score: 5/5\n"
    if marker is not None:
        body += f"\n<!-- bob-ai-review {json.dumps(marker)} -->"
    return json.dumps({"author": author, "body": body})


@pytest.fixture
def gh_comments(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Stub run_gh so the issue-comments fetch returns canned JSONL."""

    def _install(lines: list[str]) -> list[list[str]]:
        calls: list[list[str]] = []

        def fake_run_gh(args: list[str], timeout: int = 30) -> str:
            calls.append(args)
            return "\n".join(lines)

        monkeypatch.setattr(smc, "run_gh", fake_run_gh)
        return calls

    return _install


def _status(gh_comments: Any, lines: list[str], head: str = HEAD_SHA) -> dict[str, Any]:
    gh_comments(lines)
    result: dict[str, Any] = smc.fetch_ai_review_status(
        "gptme/gptme-contrib", 1382, head_sha=head, expected_author=AUTHOR
    )
    return result


# --- the case that motivated this: a clean review should satisfy the gate ---


def test_clean_current_full_consensus_review_is_accepted(gh_comments: Any) -> None:
    status = _status(gh_comments, [_comment(_marker())])
    assert status["accepted"] is True
    assert "5/5" in (status["detail"] or "")


def test_short_marker_sha_matches_full_head(gh_comments: Any) -> None:
    """The marker may abbreviate the authoritative full PR head."""
    assert smc._sha_matches_head(MARKER_SHA, HEAD_SHA)
    assert not smc._sha_matches_head(HEAD_SHA, MARKER_SHA)
    status = _status(gh_comments, [_comment(_marker(sha=HEAD_SHA))])
    assert status["accepted"] is True


def test_latest_marker_wins(gh_comments: Any) -> None:
    """A legacy append-style history reads at its most recent verdict, not its first."""
    status = _status(
        gh_comments,
        [_comment(_marker(score=2)), _comment(_marker())],
    )
    assert status["accepted"] is True


# --- recall guards: a 5/5 reached on thin evidence must NOT be accepted ---


def test_degraded_consensus_is_rejected(gh_comments: Any) -> None:
    """Lost passes: the score came from fewer samples than were requested."""
    consensus = dict(FULL_CONSENSUS, answered=2)
    status = _status(gh_comments, [_comment(_marker(consensus=consensus))])
    assert status["accepted"] is False
    assert "degraded" in (status["detail"] or "")


def test_lost_fanout_jobs_are_rejected(gh_comments: Any) -> None:
    """The loss the pass count cannot see: 3/3 passes "answered" on 3 of 9 jobs.

    A pass counts as answered when any single aspect job returns, so this run
    reports full consensus while two thirds of the evidence is missing.
    """
    consensus = dict(FULL_CONSENSUS, jobs_answered=3)
    status = _status(gh_comments, [_comment(_marker(consensus=consensus))])
    assert status["accepted"] is False
    assert "3/9 jobs" in (status["detail"] or "")


@pytest.mark.parametrize(
    ("field", "requested_field", "kind"),
    [
        ("answered", "requested", "pass"),
        ("jobs_answered", "jobs_requested", "job"),
    ],
)
def test_impossible_consensus_counts_are_rejected(
    gh_comments: Any, field: str, requested_field: str, kind: str
) -> None:
    consensus = dict(FULL_CONSENSUS)
    requested = consensus[requested_field]
    assert isinstance(requested, int)
    consensus[field] = requested + 1
    status = _status(gh_comments, [_comment(_marker(consensus=consensus))])
    assert status["accepted"] is False
    assert f"invalid {kind} counts" in (status["detail"] or "")


def test_clamped_agreement_threshold_is_rejected(gh_comments: Any) -> None:
    """A weaker filter than the one requested is not the filter that was calibrated."""
    consensus = dict(FULL_CONSENSUS, min_agreement=1)
    status = _status(gh_comments, [_comment(_marker(consensus=consensus))])
    assert status["accepted"] is False
    assert "clamped" in (status["detail"] or "")


@pytest.mark.parametrize(
    ("applied", "asked"),
    [
        (0, 0),  # unclamped, but no agreement was ever required
        (0, 2),  # clamped to nothing — caught twice over, still must fail
        (-1, -1),  # nonsense thresholds must not read as "not clamped"
        (2, 0),  # asked for nothing; `applied < asked` is False here
    ],
)
def test_sub_unit_agreement_thresholds_are_rejected(
    gh_comments: Any, applied: int, asked: int
) -> None:
    """A threshold below 1 is no consensus at all, clamped or not.

    The regression guarded here: `applied < asked` alone accepts `0/0` as
    undegraded, and since one answered pass satisfies `answered >= 1`, a
    single-pass review would enter the gate wearing a full-consensus record —
    the exact recall guard this path exists to enforce, bypassed.
    """
    consensus = dict(
        FULL_CONSENSUS, min_agreement=applied, min_agreement_requested=asked
    )
    status = _status(gh_comments, [_comment(_marker(consensus=consensus))])
    assert status["accepted"] is False
    assert "below 1" in (status["detail"] or "")


@pytest.mark.parametrize("score", ["5", 5.0, True, [5]])
def test_non_integer_score_is_named_as_a_type_defect(
    gh_comments: Any, score: Any
) -> None:
    """A malformed score must not be reported as a threshold failure.

    `AI review score 5/5 below 5/5` is false on its face and sends a reader
    hunting for a scoring bug instead of the marker corruption that caused it.
    Fails closed either way; this pins the *diagnostic*.
    """
    marker = _marker()
    marker["score"] = score
    status = _status(gh_comments, [_comment(marker)])
    assert status["accepted"] is False
    detail = status["detail"] or ""
    assert "is not an integer" in detail
    assert "below" not in detail


def test_zero_answered_passes_are_rejected(gh_comments: Any) -> None:
    """Every call failed → no findings → a clean 5/5 off an empty result."""
    consensus = dict(FULL_CONSENSUS, answered=0, jobs_answered=0)
    status = _status(gh_comments, [_comment(_marker(consensus=consensus))])
    assert status["accepted"] is False
    assert "no consensus pass answered" in (status["detail"] or "")


def test_missing_consensus_record_is_rejected(gh_comments: Any) -> None:
    """The hourly sweep runs `--passes 1`, which writes no consensus key at all.

    The reviewer documents a missing record as "unknown, never full consensus",
    and this is the marker shape actually observed live on contrib#1382.
    """
    marker = _marker()
    del marker["consensus"]
    status = _status(gh_comments, [_comment(marker)])
    assert status["accepted"] is False
    assert "no consensus record" in (status["detail"] or "")


def test_explicit_single_pass_consensus_is_rejected(gh_comments: Any) -> None:
    """A serialized 1/1 record is still a single sample, not consensus."""
    consensus = dict(
        FULL_CONSENSUS,
        requested=1,
        answered=1,
        jobs_requested=1,
        jobs_answered=1,
        min_agreement=1,
        min_agreement_requested=1,
    )
    status = _status(gh_comments, [_comment(_marker(consensus=consensus))])
    assert status["accepted"] is False
    assert "at least 2 requested passes" in (status["detail"] or "")


@pytest.mark.parametrize(
    "field",
    [
        "requested",
        "answered",
        "jobs_requested",
        "jobs_answered",
        "min_agreement",
        "min_agreement_requested",
    ],
)
def test_boolean_consensus_counts_are_rejected(gh_comments: Any, field: str) -> None:
    """Booleans are JSON-valid but are not trustworthy numeric counts."""
    consensus = dict(FULL_CONSENSUS, **{field: True})
    status = _status(gh_comments, [_comment(_marker(consensus=consensus))])
    assert status["accepted"] is False
    assert "missing" in (status["detail"] or "")


@pytest.mark.parametrize("field", ["min_agreement", "min_agreement_requested"])
def test_missing_agreement_threshold_is_rejected(gh_comments: Any, field: str) -> None:
    consensus = dict(FULL_CONSENSUS)
    del consensus[field]
    status = _status(gh_comments, [_comment(_marker(consensus=consensus))])
    assert status["accepted"] is False
    assert "missing agreement thresholds" in (status["detail"] or "")


def test_stale_sha_is_rejected(gh_comments: Any) -> None:
    """A review of an earlier push says nothing about the code about to merge."""
    status = _status(gh_comments, [_comment(_marker(sha="deadbeefcafe"))])
    assert status["accepted"] is False
    assert "stale" in (status["detail"] or "")


def test_abstain_is_rejected(gh_comments: Any) -> None:
    """`score: null` is the submodule pointer-bump case: no verdict, never a pass."""
    status = _status(gh_comments, [_comment(_marker(score=None))])
    assert status["accepted"] is False
    assert "abstain" in (status["detail"] or "")


@pytest.mark.parametrize("score", [1, 2, 3, 4])
def test_sub_clean_scores_are_rejected(gh_comments: Any, score: int) -> None:
    status = _status(gh_comments, [_comment(_marker(score=score))])
    assert status["accepted"] is False
    assert f"{score}/5" in (status["detail"] or "")


def test_marker_from_another_account_is_ignored(gh_comments: Any) -> None:
    """The marker is plain text; anyone who can comment could otherwise forge it."""
    status = _status(gh_comments, [_comment(_marker(), author="drive-by-contributor")])
    assert status["accepted"] is False
    assert status["detail"] is None


def test_unknown_identity_trusts_nothing(gh_comments: Any) -> None:
    gh_comments([_comment(_marker())])
    status = smc.fetch_ai_review_status(
        "gptme/gptme-contrib", 1382, head_sha=HEAD_SHA, expected_author=""
    )
    assert status["accepted"] is False


def test_no_marker_at_all(gh_comments: Any) -> None:
    status = _status(gh_comments, [_comment(None)])
    assert status["accepted"] is False
    assert status["detail"] is None


def test_inline_finding_marker_is_not_a_summary_marker() -> None:
    """`-finding` and `-fp {..}` share the prefix; only `bob-ai-review {` counts."""
    assert not list(smc._iter_ai_review_markers(smc._AI_REVIEW_FINDING_MARKER))
    assert not list(smc._iter_ai_review_markers('<!-- bob-ai-review-fp {"a": 1} -->'))
    assert list(smc._iter_ai_review_markers('<!-- bob-ai-review {"sha": "x"} -->'))


def test_nested_json_marker_parses_whole() -> None:
    """The real marker nests objects; parsing must reach the top-level close."""
    marker = _marker(history=[{"sha": "aaaabbbbcccc", "score": 3}])
    parsed = list(
        smc._iter_ai_review_markers(
            f"text\n<!-- bob-ai-review {json.dumps(marker)} -->\n"
        )
    )
    assert parsed[0]["consensus"]["jobs_answered"] == 9


def test_multiple_markers_and_malformed_marker_recover() -> None:
    body = "\n".join(
        [
            '<!-- bob-ai-review {"broken": } -->',
            '<!-- bob-ai-review {"score": 3} -->',
            '<!-- bob-ai-review {"score": 5, "nested": {"ok": true}} -->',
        ]
    )
    assert [marker["score"] for marker in smc._iter_ai_review_markers(body)] == [3, 5]


def test_truncated_sha_cannot_prefix_match_any_head() -> None:
    assert not smc._sha_matches_head("9a", HEAD_SHA)
    assert not smc._sha_matches_head("", HEAD_SHA)
    assert not smc._sha_matches_head(MARKER_SHA, "")


def test_kill_switch_restores_greptile_only(
    gh_comments: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(smc.SELF_MERGE_ACCEPT_AI_REVIEW_ENV, "0")
    status = _status(gh_comments, [_comment(_marker())])
    assert status["accepted"] is False


def test_enabled_by_default_and_for_non_falsey_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(smc.SELF_MERGE_ACCEPT_AI_REVIEW_ENV, raising=False)
    assert smc._ai_review_enabled()
    monkeypatch.setenv(smc.SELF_MERGE_ACCEPT_AI_REVIEW_ENV, "1")
    assert smc._ai_review_enabled()
    monkeypatch.setenv(smc.SELF_MERGE_ACCEPT_AI_REVIEW_ENV, "off")
    assert not smc._ai_review_enabled()


# --- end-to-end through evaluate_pr, including the untouched Greptile path ---


def _pr_payload() -> dict[str, Any]:
    return {
        "number": 1382,
        "title": "docs: cross-reference the review implementations",
        "url": "https://github.com/gptme/gptme-contrib/pull/1382",
        "author": {"login": AUTHOR},
        "headRefOid": HEAD_SHA,
        "isDraft": False,
        "state": "OPEN",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
        "reviewDecision": None,
        "files": [{"path": "docs/review-tools.md"}],
    }


@pytest.fixture
def evaluate(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Drive evaluate_pr with everything but the review signal stubbed out."""

    def _run(
        *, greptile: dict[str, Any], comments: list[str]
    ) -> tuple[Any, list[list[str]]]:
        calls: list[list[str]] = []

        def fake_run_gh(args: list[str], timeout: int = 30) -> str:
            calls.append(args)
            return "\n".join(comments)

        monkeypatch.setattr(smc, "run_gh", fake_run_gh)
        monkeypatch.setattr(smc, "fetch_pr", lambda repo, number: _pr_payload())
        monkeypatch.setattr(smc, "get_gh_user", lambda: AUTHOR)
        monkeypatch.setattr(smc, "merge_permission", lambda repo: True)
        monkeypatch.setattr(smc, "_fetch_greptile_review_data", lambda r, n: ([], []))
        monkeypatch.setattr(
            smc, "fetch_greptile_status", lambda r, n, review_data=None: greptile
        )
        monkeypatch.setattr(
            smc,
            "fetch_unresolved_human_threads",
            lambda r, n, review_data=None: {
                "unresolved": 0,
                "total": 0,
                "authors": [],
            },
        )
        monkeypatch.setattr(smc, "greptile_summary_score", lambda r, n: 5)
        result = smc.evaluate_pr(
            "gptme/gptme-contrib", 1382, workspace_repos=["gptme/gptme-contrib"]
        )
        return result, calls

    return _run


NO_GREPTILE = {"has_review": False, "unresolved": 0, "total": 0}
GREPTILE_CLEAN = {"has_review": True, "unresolved": 0, "total": 0}


def test_evaluate_pr_accepts_our_clean_review_without_greptile(evaluate: Any) -> None:
    """The #1382 regression: eligible on our own review, no Greptile anywhere."""
    result, _ = evaluate(greptile=NO_GREPTILE, comments=[_comment(_marker())])
    assert result.eligible is True
    assert result.reasons == []
    assert any("satisfied by self-hosted AI review" in w for w in result.warnings)


def test_evaluate_pr_blocks_fallback_when_greptile_state_fetch_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown Greptile state must not become absence and admit our review."""
    monkeypatch.setattr(smc, "fetch_pr", lambda repo, number: _pr_payload())
    monkeypatch.setattr(smc, "get_gh_user", lambda: AUTHOR)
    monkeypatch.setattr(smc, "merge_permission", lambda repo: True)
    monkeypatch.setattr(smc, "_fetch_greptile_review_data", lambda r, n: None)
    monkeypatch.setattr(smc, "greptile_summary_score", lambda r, n: 5)
    result = smc.evaluate_pr(
        "gptme/gptme-contrib", 1382, workspace_repos=["gptme/gptme-contrib"]
    )
    assert result.eligible is False
    assert any("Could not verify Greptile review state" in r for r in result.reasons)


def test_evaluate_pr_blocks_on_degraded_review(evaluate: Any) -> None:
    consensus = dict(FULL_CONSENSUS, jobs_answered=4)
    result, _ = evaluate(
        greptile=NO_GREPTILE, comments=[_comment(_marker(consensus=consensus))]
    )
    assert result.eligible is False
    assert any("Greptile review not found" in r for r in result.reasons)
    assert any("degraded" in r for r in result.reasons)


def test_evaluate_pr_blocks_on_stale_review(evaluate: Any) -> None:
    result, _ = evaluate(
        greptile=NO_GREPTILE, comments=[_comment(_marker(sha="deadbeefcafe"))]
    )
    assert result.eligible is False
    assert any("stale" in r for r in result.reasons)


def test_evaluate_pr_blocks_on_abstain(evaluate: Any) -> None:
    result, _ = evaluate(greptile=NO_GREPTILE, comments=[_comment(_marker(score=None))])
    assert result.eligible is False
    assert any("abstain" in r for r in result.reasons)


def test_evaluate_pr_reason_unchanged_when_no_ai_review(evaluate: Any) -> None:
    """No reviewer at all still reads exactly as it did before this change."""
    result, _ = evaluate(greptile=NO_GREPTILE, comments=[_comment(None)])
    assert result.eligible is False
    assert "Greptile review not found" in result.reasons


def test_greptile_path_is_untouched_and_pays_no_extra_call(evaluate: Any) -> None:
    """It's an OR: when Greptile reviewed, the marker is never even fetched."""
    result, calls = evaluate(greptile=GREPTILE_CLEAN, comments=[_comment(_marker())])
    assert result.eligible is True
    assert not any("issues/1382/comments" in " ".join(c) for c in calls)


def test_greptile_unresolved_still_blocks_despite_clean_ai_review(
    evaluate: Any,
) -> None:
    """Our review is an alternative to a *missing* Greptile, never an override."""
    greptile = {"has_review": True, "unresolved": 2, "total": 3}
    result, _ = evaluate(greptile=greptile, comments=[_comment(_marker())])
    assert result.eligible is False
    assert any("2 unresolved review thread(s)" in r for r in result.reasons)


@pytest.mark.parametrize("score", [6, 7, 100])
def test_score_above_the_rubric_is_rejected(gh_comments: Any, score: int) -> None:
    """`confidence_score` emits 1-5; a 6 is corruption, not a better review.

    `score < AI_REVIEW_CLEAN_SCORE` waved these through as clean while author,
    SHA and consensus checks all still passed — one corrupted numeric field was
    enough to satisfy the whole alternative path.
    """
    status = _status(gh_comments, [_comment(_marker(score=score))])
    assert status["accepted"] is False
    assert "outside the rubric" in (status["detail"] or "")


def test_failed_summary_comment_fetch_is_unknown_not_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient comment-API failure must not open the AI-review fallback.

    `run_gh` returns "" for both "no Greptile comment" and "the call failed", so
    `fetch_greptile_status` reported `has_review: False` on a timeout. That is
    the same shape as a genuinely un-reviewed PR, and `evaluate_pr` only guarded
    on the GraphQL fetch — so a flaky comment fetch could hand the AI marker a
    PR that Greptile had unresolved feedback on.
    """
    monkeypatch.setattr(smc, "run_gh_checked", lambda *a, **k: None)
    status = smc.fetch_greptile_status(
        "gptme/gptme-contrib", 1382, review_data=([], [])
    )
    assert status["has_review"] is False
    assert status["unknown"] is True


def test_empty_summary_comment_fetch_is_absence_not_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the same coin: a real empty result stays actionable."""
    monkeypatch.setattr(smc, "run_gh_checked", lambda *a, **k: "")
    status = smc.fetch_greptile_status(
        "gptme/gptme-contrib", 1382, review_data=([], [])
    )
    assert status["has_review"] is False
    assert not status.get("unknown")
