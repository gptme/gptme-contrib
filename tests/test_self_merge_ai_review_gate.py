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
    """The marker records 12 chars; the PR carries 40. Prefix match, either way."""
    assert smc._sha_matches_head(MARKER_SHA, HEAD_SHA)
    assert smc._sha_matches_head(HEAD_SHA, MARKER_SHA)
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


def test_clamped_agreement_threshold_is_rejected(gh_comments: Any) -> None:
    """A weaker filter than the one requested is not the filter that was calibrated."""
    consensus = dict(FULL_CONSENSUS, min_agreement=1)
    status = _status(gh_comments, [_comment(_marker(consensus=consensus))])
    assert status["accepted"] is False
    assert "clamped" in (status["detail"] or "")


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
    assert not smc._AI_REVIEW_SUMMARY_RE.search(smc._AI_REVIEW_FINDING_MARKER)
    assert not smc._AI_REVIEW_SUMMARY_RE.search('<!-- bob-ai-review-fp {"a": 1} -->')
    assert smc._AI_REVIEW_SUMMARY_RE.search('<!-- bob-ai-review {"sha": "x"} -->')


def test_nested_json_marker_parses_whole() -> None:
    """The real marker nests objects in `history`; a lazy match must not stop early."""
    marker = _marker(history=[{"sha": "aaaabbbbcccc", "score": 3}])
    match = smc._AI_REVIEW_SUMMARY_RE.search(
        f"text\n<!-- bob-ai-review {json.dumps(marker)} -->\n"
    )
    assert match is not None
    assert json.loads(match.group(1))["consensus"]["jobs_answered"] == 9


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
