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
    """A clean, current, full-consensus marker as the reviewer writes it.

    No ``findings`` key by default: that is the legacy marker shape, and it is
    what exercises the score/severity-count guard. Tests for per-fingerprint
    matching pass ``findings=[...]`` explicitly — and must pass one consistent
    with the score they set, since a marker claiming a P0 while publishing no
    P0 entry is refused as self-contradictory rather than downgraded."""
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
    """Stub the gh helpers so the issue-comments fetch returns canned JSONL.

    Both ``run_gh`` and ``run_gh_checked`` are stubbed: the marker fetch uses the
    checked variant so it can tell a failed call from an empty one, while other
    call sites still use the plain one.
    """

    def _install(lines: list[str]) -> list[list[str]]:
        calls: list[list[str]] = []

        def fake_run_gh(args: list[str], timeout: int = 30) -> str:
            calls.append(args)
            return "\n".join(lines)

        monkeypatch.setattr(smc, "run_gh", fake_run_gh)
        monkeypatch.setattr(smc, "run_gh_checked", fake_run_gh)
        return calls

    return _install


def _status(
    gh_comments: Any,
    lines: list[str],
    head: str = HEAD_SHA,
    review_data: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = (
        [],
        [],
    ),
) -> dict[str, Any]:
    gh_comments(lines)
    result: dict[str, Any] = smc.fetch_ai_review_status(
        "gptme/gptme-contrib",
        1382,
        head_sha=head,
        expected_author=AUTHOR,
        review_data=review_data,
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


@pytest.mark.parametrize("score", [1, 2, 3])
def test_p0p1_scores_are_rejected_when_no_thread_proves_disposition(
    gh_comments: Any, score: int
) -> None:
    """Scores claiming a P0/P1 on this head block unless a finding thread proves
    it was disposed. With no finding thread at all (unanchored findings, or a
    reviewer that stopped posting), we cannot verify disposition — fail closed."""
    status = _status(gh_comments, [_comment(_marker(score=score))])
    assert status["accepted"] is False
    assert "disposed" in (status["detail"] or "")


def test_p2_only_score_does_not_block(gh_comments: Any) -> None:
    """A 4/5 means only P2 findings survive. Those demonstrably regenerate on
    unchanged code (a frozen head scored 4,5,4,5,4 across six passes with zero
    code changes), so they must not block — this is the core of replacing the
    literal 5/5 score floor with a disposition check."""
    status = _status(gh_comments, [_comment(_marker(score=4))])
    assert status["accepted"] is True


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
        monkeypatch.setattr(smc, "run_gh_checked", fake_run_gh)
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


def test_greptile_path_is_untouched_and_pays_one_marker_fetch(evaluate: Any) -> None:
    """It's an OR: a Greptile review still decides the outcome on its own.

    The marker *is* fetched now, because the abstention blocker has to read it on
    every PR regardless of Greptile. What matters is that it costs exactly one
    fetch shared by both readers, not one each, and that it does not change the
    verdict on the Greptile path.
    """
    result, calls = evaluate(greptile=GREPTILE_CLEAN, comments=[_comment(_marker())])
    assert result.eligible is True
    marker_fetches = [c for c in calls if "issues/1382/comments" in " ".join(c)]
    assert len(marker_fetches) == 1


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


# --- disposition-based acceptance (replaces the 5/5 score floor) ------------
#
# The literal `score == 5` floor is a sampling event, not a converged state:
# measured on gptme-contrib#1393 with the head frozen, six re-review passes
# with zero code changes scored 4,5,4,5,4. So the gate now reads the finding
# threads directly: no open P0/P1, every P0/P1 finding addressed (replied) AND
# disposed (resolved), and surviving P2s never block.


def _finding_thread(
    severity: str, *, resolved: bool, total: int, fp: str | None = None
) -> dict[str, Any]:
    """One AI finding thread with the reviewer's marker, severity, and replies."""
    fp_line = f'<!-- bob-ai-review-fp {{"fp": "{fp}"}} -->\n' if fp else ""
    body = (
        f"{smc._AI_REVIEW_FINDING_MARKER}\n"
        f"{fp_line}"
        f"🛑 **{severity}** — the hash ignores .state"
    )
    return {
        "isResolved": resolved,
        "comments": {
            "totalCount": total,
            "nodes": [{"author": {"login": AUTHOR}, "body": body}],
        },
    }


def test_open_p0_finding_blocks(gh_comments: Any) -> None:
    """A P0 finding thread that is not resolved hard-blocks, whatever the score."""
    status = _status(
        gh_comments,
        [_comment(_marker(score=1))],
        review_data=([], [_finding_thread("P0", resolved=False, total=1)]),
    )
    assert status["accepted"] is False
    assert "not resolved" in (status["detail"] or "")


def test_open_p1_finding_blocks(gh_comments: Any) -> None:
    status = _status(
        gh_comments,
        [_comment(_marker(score=3))],
        review_data=([], [_finding_thread("P1", resolved=False, total=1)]),
    )
    assert status["accepted"] is False
    assert "P1" in (status["detail"] or "")
    assert "not resolved" in (status["detail"] or "")


def test_resolved_p1_with_reply_is_accepted(gh_comments: Any) -> None:
    """A P1 that was replied to AND resolved is disposed — that is the gate."""
    status = _status(
        gh_comments,
        [_comment(_marker(score=3))],
        review_data=([], [_finding_thread("P1", resolved=True, total=2)]),
    )
    assert status["accepted"] is True


def test_resolved_without_reply_blocks(gh_comments: Any) -> None:
    """The #1389 failure: a resolved P0/P1 thread with no reply is not a
    disposition. Resolution alone is not evidence anybody addressed it."""
    status = _status(
        gh_comments,
        [_comment(_marker(score=3))],
        review_data=([], [_finding_thread("P1", resolved=True, total=1)]),
    )
    assert status["accepted"] is False
    assert "without a reply" in (status["detail"] or "")


def test_resolved_without_reply_but_auto_resolved_is_accepted(
    gh_comments: Any,
) -> None:
    """The reviewer itself retires a finding when a fix makes it stop
    reproducing — the marker's `auto_resolved` ledger names those fingerprints.
    That is evidence-based (the code change is the answer), so no reply is owed.

    Scored 4, not 3, because those are the states that can actually coexist: the
    retirement and the score are written by the same reviewer run, so a run that
    retired the P1 no longer reports one. A score of 3 alongside nothing but
    auto-resolved threads is a contradiction, and the recall guard blocks it —
    see `test_auto_resolved_thread_does_not_satisfy_the_recall_guard`."""
    fp = "abcdef123456"
    body = (
        f"{smc._AI_REVIEW_FINDING_MARKER}\n"
        f'<!-- bob-ai-review-fp {{"fp": "{fp}"}} -->\n'
        f"🛑 **P1** — the hash ignores .state"
    )
    thread = {
        "isResolved": True,
        "comments": {
            "totalCount": 1,
            "nodes": [{"author": {"login": AUTHOR}, "body": body}],
        },
    }
    marker = _marker(score=4, auto_resolved=[fp])
    status = _status(gh_comments, [_comment(marker)], review_data=([], [thread]))
    assert status["accepted"] is True


def test_surviving_p2_never_blocks(gh_comments: Any) -> None:
    """Even an OPEN P2 finding thread must not block — P2s regenerate on
    unchanged code, so gating on them is the treadmill failure mode."""
    status = _status(
        gh_comments,
        [_comment(_marker(score=4))],
        review_data=([], [_finding_thread("P2", resolved=False, total=1)]),
    )
    assert status["accepted"] is True


def test_unanchored_p0p1_fails_closed(gh_comments: Any) -> None:
    """A score <= 3 with no P0/P1 finding thread at all is unverifiable. The
    finding may be unanchored (only in the summary body) or the reviewer may
    have stopped posting. Fail closed rather than merge an unverified P0/P1."""
    status = _status(
        gh_comments,
        [_comment(_marker(score=2))],
        review_data=([], []),
    )
    assert status["accepted"] is False
    assert "disposed" in (status["detail"] or "")


def test_unreadable_thread_state_fails_closed(gh_comments: Any) -> None:
    """review_data=None (fetch failed) is unknown, never clean — same fail-closed
    property as the Greptile fallback."""
    status = _status(gh_comments, [_comment(_marker())], review_data=None)
    assert status["accepted"] is False
    assert "could not read" in (status["detail"] or "")


def test_p0p1_score_with_all_disposed_passes(gh_comments: Any) -> None:
    """A score of 3 (one P1 reported) is acceptable when the P1 thread proves
    it was replied to and resolved — the disposition is the truth, not the
    score."""
    status = _status(
        gh_comments,
        [_comment(_marker(score=3))],
        review_data=([], [_finding_thread("P1", resolved=True, total=2)]),
    )
    assert status["accepted"] is True
    assert "findings disposed" in (status["detail"] or "")


# --- fail-closed on unreadable severity, and the recall guard's blind spot ---


def _finding_thread_body(
    severity_text: str, *, fp: str | None = None, resolved: bool, total: int
) -> dict[str, Any]:
    """A finding thread whose severity text is written verbatim, so a test can
    render a malformed / missing severity the way a protocol drift would."""
    fp_line = f'<!-- bob-ai-review-fp {{"fp": "{fp}"}} -->\n' if fp else ""
    body = f"{smc._AI_REVIEW_FINDING_MARKER}\n{fp_line}{severity_text}"
    return {
        "isResolved": resolved,
        "comments": {
            "totalCount": total,
            "nodes": [{"author": {"login": AUTHOR}, "body": body}],
        },
    }


def test_finding_without_severity_text_blocks_instead_of_defaulting_to_p2(
    gh_comments: Any,
) -> None:
    """A thread carrying our finding marker whose severity does not parse is NOT
    a P2. The renderer that writes `**P0**` lives in another repo, so a body
    without it means the wire protocol drifted or the body is malformed —
    downgrading a possible P0 to non-blocking would be a fail-open on the parse.
    """
    status = _status(
        gh_comments,
        [_comment(_marker(score=5))],
        review_data=(
            [],
            [
                _finding_thread_body(
                    "Security vulnerability in auth", resolved=False, total=1
                )
            ],
        ),
    )
    assert status["accepted"] is False
    assert "unreadable severity" in (status["detail"] or "")


def test_finding_without_severity_text_can_still_be_disposed(
    gh_comments: Any,
) -> None:
    """Blocking on an unreadable severity must stay clearable the same way a
    P0/P1 is — replied to and resolved — not become a permanent wedge."""
    status = _status(
        gh_comments,
        [_comment(_marker(score=5))],
        review_data=(
            [],
            [
                _finding_thread_body(
                    "Security vulnerability in auth", resolved=True, total=2
                )
            ],
        ),
    )
    assert status["accepted"] is True


def test_severity_below_p1_still_never_blocks(gh_comments: Any) -> None:
    """A readable severity outside P0/P1 (P2 today, P3 if the rubric grows) is
    non-blocking. The fail-closed rule above applies only to severities we
    cannot read at all, so widening the rubric does not wedge the gate."""
    status = _status(
        gh_comments,
        [_comment(_marker(score=4))],
        review_data=(
            [],
            [_finding_thread_body("⚠️ **P3** — nit", resolved=False, total=1)],
        ),
    )
    assert status["accepted"] is True


def test_auto_resolved_thread_does_not_satisfy_the_recall_guard(
    gh_comments: Any,
) -> None:
    """`auto_resolved` is the reviewer's own record that a finding stopped
    reproducing and it retired the thread as bookkeeping. Such a thread is by
    construction not the P0/P1 the score reports for THIS head, so it must not
    stand in for a real disposition — otherwise the reviewer's bookkeeping
    silently disarms the recall guard for an unanchored finding."""
    fp = "abcdef123456"
    status = _status(
        gh_comments,
        [
            _comment(
                _marker(
                    score=2,
                    auto_resolved=[fp],
                    findings=[
                        {"fp": fp, "severity": "P1"},
                        {"fp": "feedface5678", "severity": "P1"},
                    ],
                )
            )
        ],
        review_data=(
            [],
            [
                _finding_thread_body(
                    "🛑 **P1** — the hash ignores .state",
                    fp=fp,
                    resolved=True,
                    total=1,
                )
            ],
        ),
    )
    assert status["accepted"] is False
    assert "current marker findings lack disposed threads for: 2 P1" in (
        status["detail"] or ""
    )


def test_refusal_detail_is_not_double_prefixed(gh_comments: Any) -> None:
    """The caller prefixes `AI review `; the shortfall returns a bare fragment.
    Both prefixing produced `AI review AI review P1 finding is not resolved`."""
    status = _status(
        gh_comments,
        [
            _comment(
                _marker(score=3, findings=[{"fp": "cafe1234beef", "severity": "P1"}])
            )
        ],
        review_data=(
            [],
            [_finding_thread("P1", resolved=False, total=1, fp="cafe1234beef")],
        ),
    )
    detail = status["detail"] or ""
    assert detail.count("AI review") == 1, detail
    assert detail == "AI review P1 finding is not resolved"


# --- the recall guard must match the finding the SCORE claims ---------------
#
# `confidence_score` is arithmetic over the finding severities, so a score
# names its findings exactly: 1 = any P0, 2 = 2+ P1, 3 = exactly one P1. A
# single "did we see a P0/P1 thread" boolean let one disposed P1 vouch for a
# different, unanchored P0.


def test_disposed_p1_does_not_vouch_for_an_unanchored_p0(gh_comments: Any) -> None:
    """A score of 1 means the reviewer found a P0 on this head. A resolved and
    replied-to *P1* thread does not verify that P0 was addressed — if the P0
    was unanchored ("Comments outside the diff"), accepting here merges it
    unreviewed, which is exactly what the guard exists to stop."""
    status = _status(
        gh_comments,
        [
            _comment(
                _marker(score=1, findings=[{"fp": "c0ffee123abc", "severity": "P0"}])
            )
        ],
        review_data=(
            [],
            [_finding_thread("P1", resolved=True, total=2, fp="deadbeef1234")],
        ),
    )
    assert status["accepted"] is False
    assert "current marker findings lack disposed threads for: 1 P0" in (
        status["detail"] or ""
    )


def test_two_p1_score_needs_two_disposed_p1_threads(gh_comments: Any) -> None:
    """A score of 2 means two or more P1s. One disposed P1 thread leaves the
    other unaccounted for, so the count has to match, not just the severity."""
    marker = _marker(
        score=2,
        findings=[
            {"fp": "a1b2c3d4e5f6", "severity": "P1"},
            {"fp": "b1c2d3e4f5a6", "severity": "P1"},
        ],
    )
    one_disposed = _status(
        gh_comments,
        [_comment(marker)],
        review_data=(
            [],
            [_finding_thread("P1", resolved=True, total=2, fp="a1b2c3d4e5f6")],
        ),
    )
    assert one_disposed["accepted"] is False
    assert "current marker findings lack disposed threads for: 1 P1" in (
        one_disposed["detail"] or ""
    )

    both_disposed = _status(
        gh_comments,
        [_comment(marker)],
        review_data=(
            [],
            [
                _finding_thread("P1", resolved=True, total=2, fp="a1b2c3d4e5f6"),
                _finding_thread("P1", resolved=True, total=2, fp="b1c2d3e4f5a6"),
            ],
        ),
    )
    assert both_disposed["accepted"] is True


def test_disposed_p0_satisfies_a_p0_score(gh_comments: Any) -> None:
    """The guard is a correspondence check, not a ban on low scores: a score of
    1 with its P0 replied to and resolved is disposed, and passes."""
    status = _status(
        gh_comments,
        [
            _comment(
                _marker(score=1, findings=[{"fp": "facefeed1234", "severity": "P0"}])
            )
        ],
        review_data=(
            [],
            [_finding_thread("P0", resolved=True, total=2, fp="facefeed1234")],
        ),
    )
    assert status["accepted"] is True


def test_unreadable_severity_thread_does_not_credit_a_claimed_p0(
    gh_comments: Any,
) -> None:
    """A disposed thread whose severity did not parse could have been anything.
    Crediting it against a P0 the score reports would be guessing in the merge
    direction, so it clears its own block without vouching for the P0."""
    status = _status(
        gh_comments,
        [_comment(_marker(score=1))],
        review_data=(
            [],
            [_finding_thread_body("Security vulnerability", resolved=True, total=2)],
        ),
    )
    assert status["accepted"] is False
    assert "only 0 disposed P0" in (status["detail"] or "")


# --- score corruption is bounded at BOTH ends -------------------------------


@pytest.mark.parametrize("score", [0, -1, -5])
def test_scores_below_the_rubric_are_rejected(gh_comments: Any, score: int) -> None:
    """`confidence_score` emits 1-5; abstain is `None`, never 0. Replacing the
    old `score != 5` equality with an upper bound alone would newly admit 0 and
    negatives, which then read as "a bad score the disposition check can clear"
    rather than as the impossible markers they are — and with one disposed
    P0/P1 thread present they would have been accepted."""
    status = _status(
        gh_comments,
        [_comment(_marker(score=score))],
        review_data=([], [_finding_thread("P1", resolved=True, total=2)]),
    )
    assert status["accepted"] is False
    assert "outside the rubric" in (status["detail"] or "")


# --- finding threads must be OURS, not merely marker-shaped ------------------


def _foreign_finding_thread(severity: str, *, author: str) -> dict[str, Any]:
    """A thread that looks exactly like one of our findings but was written by
    somebody else — the marker is a label, not a signature."""
    body = f"{smc._AI_REVIEW_FINDING_MARKER}\n🛑 **{severity}** — forged"
    return {
        "isResolved": True,
        "comments": {
            "totalCount": 2,
            "nodes": [{"author": {"login": author}, "body": body}],
        },
    }


def test_forged_finding_thread_does_not_dispose_a_claimed_p0(
    gh_comments: Any,
) -> None:
    """Anyone with write access can paste the finding marker and `**P0**` into a
    review thread, resolve it and reply once. Counting that as a disposition
    hands the recall guard a forged answer for a P0 that was never addressed —
    so a thread not authored by our reviewer is not one of our findings."""
    status = _status(
        gh_comments,
        [_comment(_marker(score=1))],
        review_data=([], [_foreign_finding_thread("P0", author="mallory")]),
    )
    assert status["accepted"] is False
    assert "only 0 disposed P0" in (status["detail"] or "")


def test_our_own_finding_thread_still_counts(gh_comments: Any) -> None:
    """Negative control for the author check: the same thread from the reviewer
    itself disposes the P0 exactly as before."""
    status = _status(
        gh_comments,
        [_comment(_marker(score=1))],
        review_data=([], [_foreign_finding_thread("P0", author=AUTHOR)]),
    )
    assert status["accepted"] is True


def test_forged_open_finding_thread_does_not_block_either(gh_comments: Any) -> None:
    """The check cuts both ways and must not become a griefing vector: a forged
    *unresolved* P0 is not our finding, so it cannot wedge the AI-review path.
    (It is still an unresolved thread to the human-thread check, which is where
    a stranger's review comment belongs.)"""
    thread = _foreign_finding_thread("P0", author="mallory")
    thread["isResolved"] = False
    status = _status(
        gh_comments,
        [_comment(_marker(score=5))],
        review_data=([], [thread]),
    )
    assert status["accepted"] is True


# --- a PRESENT `findings` key is authoritative, even empty or unreadable -----


def test_empty_findings_list_does_not_fall_back_to_the_score_guard(
    gh_comments: Any,
) -> None:
    """`findings: []` with `score: 1` is a self-contradictory marker. Reading
    the empty list as "key absent" drops to the score-only guard, which accepts
    any disposed P0 thread — so a stale, already-disposed P0 would clear a head
    whose own P0 was never addressed. Presence is the signal, not truthiness."""
    status = _status(
        gh_comments,
        [_comment(_marker(score=1, findings=[]))],
        review_data=([], [_finding_thread("P0", resolved=True, total=2)]),
    )
    assert status["accepted"] is False
    assert "publishes 0 P0 finding(s)" in (status["detail"] or "")


def test_findings_entry_without_a_fingerprint_fails_closed(gh_comments: Any) -> None:
    """Silently skipping an unreadable entry shrinks the set of dispositions
    demanded — the fail-open direction. An entry we cannot read is refused."""
    status = _status(
        gh_comments,
        [_comment(_marker(score=1, findings=[{"severity": "P0"}]))],
        review_data=([], [_finding_thread("P0", resolved=True, total=2)]),
    )
    assert status["accepted"] is False
    assert "cannot read" in (status["detail"] or "")


def test_findings_must_account_for_the_score(gh_comments: Any) -> None:
    """A score of 2 means two or more P1s; publishing one P1 entry contradicts
    it, so the marker is refused rather than half-believed."""
    fp = "aaaabbbbcccc"
    status = _status(
        gh_comments,
        [_comment(_marker(score=2, findings=[{"fp": fp, "severity": "P1"}]))],
        # The one published finding IS disposed, so this isolates the score
        # check from the per-fingerprint one.
        review_data=(
            [],
            [
                _finding_thread_body(
                    "\u274c **P1** \u2014 the hash ignores .state",
                    fp=fp,
                    resolved=True,
                    total=2,
                )
            ],
        ),
    )
    assert status["accepted"] is False
    assert "publishes 1 P1 finding(s), not 2" in (status["detail"] or "")


def test_missing_findings_key_still_uses_the_legacy_score_guard(
    gh_comments: Any,
) -> None:
    """Negative control: markers written before `findings` existed must keep
    working, or the gate breaks on every PR reviewed by an older reviewer."""
    marker = _marker(score=3)
    assert "findings" not in marker
    status = _status(
        gh_comments,
        [_comment(marker)],
        review_data=([], [_finding_thread("P1", resolved=True, total=2)]),
    )
    assert status["accepted"] is True


def test_disposed_p1_thread_does_not_satisfy_a_p0_findings_entry(
    gh_comments: Any,
) -> None:
    """A fingerprint outlives a severity. The reviewer can re-raise the same
    finding at a higher severity on a later head while the old, already-disposed
    thread still renders the OLD severity — and matching on fingerprint alone
    then credits a P0 entry to a disposed P1 thread. That is reachable exactly
    when the re-raised P0 could not be anchored to a diff line, i.e. when this
    guard is the only thing still checking."""
    fp = "deadbeef1234"
    status = _status(
        gh_comments,
        [_comment(_marker(score=1, findings=[{"fp": fp, "severity": "P0"}]))],
        review_data=(
            [],
            [
                _finding_thread_body(
                    "❌ **P1** — the hash ignores .state",
                    fp=fp,
                    resolved=True,
                    total=2,
                )
            ],
        ),
    )
    assert status["accepted"] is False
    assert "1 P0" in (status["detail"] or "")


def test_matching_severity_still_disposes(gh_comments: Any) -> None:
    """Negative control: the same fingerprint with the severity the marker
    actually claims disposes it, so the tightening costs nothing legitimate."""
    fp = "deadbeef1234"
    status = _status(
        gh_comments,
        [_comment(_marker(score=1, findings=[{"fp": fp, "severity": "P0"}]))],
        review_data=(
            [],
            [
                _finding_thread_body(
                    "🛑 **P0** — the hash ignores .state",
                    fp=fp,
                    resolved=True,
                    total=2,
                )
            ],
        ),
    )
    assert status["accepted"] is True
