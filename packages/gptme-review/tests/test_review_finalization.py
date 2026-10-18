"""Explicit finalization policy, independent of inference and forge writes."""

from copy import deepcopy

import pytest
from gptme_review import core


def finding(file="a.py", severity="P1", finding_class="bug"):
    return {
        "file": file,
        "line": 1,
        "severity": severity,
        "class": finding_class,
        "body": "The returned counter is two instead of one, breaking callers using it as an index.",
        "quote": "count = 2",
    }


def finalize(findings, **kwargs):
    return core.finalize_review(
        findings,
        change_summary={},
        fully_preloaded_paths={"a.py"},
        file_contents={"a.py": "count = 2\n"},
        touched_files={"a.py"},
        **kwargs,
    )


@pytest.mark.parametrize("policy,score", [("suppress", None), ("informational", 5)])
def test_scope_policy_preserves_characterized_local_and_sweep_verdicts(policy, score):
    observations = [finding("untouched.py")]
    before = deepcopy(observations)
    result = finalize(observations, out_of_scope_policy=policy)
    assert result.findings == []
    assert result.out_of_scope_findings == observations
    assert result.score == score
    assert not result.blocking
    assert result.shortfalls == []
    assert observations == before


@pytest.mark.parametrize(
    "severity,score,blocking", [("P0", 1, True), ("P1", 3, True), ("P2", 4, False)]
)
def test_in_scope_verdicts_and_advisories(severity, score, blocking):
    bug = finding(severity=severity)
    advisory = {
        **finding(severity="P2", finding_class="guard"),
        "body": "A defensive input bound check could reject unexpected counter values.",
    }
    result = finalize([bug, advisory], out_of_scope_policy="suppress")
    assert result.findings == [bug]
    assert result.summary_only_findings == [advisory]
    assert result.score == score
    assert result.blocking is blocking
    assert bool(result.shortfalls) is blocking


def test_author_rejections_retain_evidence_and_do_not_grant_clean_score():
    bug = finding()
    rejection = {
        "entry": core.suppression_entry(bug),
        "reason": "Accepted tradeoff",
        "at": "2026-10-18",
    }
    before = deepcopy(rejection)
    result = finalize(
        [bug],
        out_of_scope_policy="informational",
        author_rejections=[{"entry": None}, rejection],
    )
    assert result.findings == []
    assert result.rejected_findings == [
        {**bug, "author_rejected": {"reason": "Accepted tradeoff", "at": "2026-10-18"}}
    ]
    assert result.score is None
    assert not result.blocking
    assert rejection == before


def test_matching_retains_fuzzy_reworded_author_rejections():
    original = finding()
    reworded = {**original, "body": original["body"] + " This corrupts the result."}
    assert core.finding_fingerprint(original) != core.finding_fingerprint(reworded)
    result = finalize(
        [reworded],
        out_of_scope_policy="suppress",
        author_rejections=[{"entry": core.suppression_entry(original)}],
    )
    assert result.findings == []
    assert len(result.rejected_findings) == 1


def test_misquotes_are_invalid_evidence_not_suppression():
    invalid = {**finding(), "quote": "missing = True"}
    result = finalize([invalid], out_of_scope_policy="suppress")
    assert result.findings == []
    assert result.dropped["misquoted"] == [invalid]
    assert result.score == 5
    assert not result.blocking


def test_absence_check_precedes_misquote_and_scope_policy():
    invalid = {
        **finding("untouched.py"),
        "body": "The counter is never read anywhere in this module.",
        "quote": "missing = True",
    }
    result = finalize([invalid], out_of_scope_policy="suppress")
    assert len(result.dropped["unverified_absence"]) == 1
    assert result.dropped["misquoted"] == []
    assert result.out_of_scope_findings == []
    assert result.score == 5


def test_complete_file_coverage_is_not_guessed_from_preloaded_excerpts():
    claim = {
        **finding("src/a.py"),
        "body": "The counter is never read anywhere in this module.",
        "verification": "Read src/a.py in full.",
    }
    complete = core.finalize_review(
        [claim],
        change_summary={},
        fully_preloaded_paths={"src/a.py"},
        file_contents={"src/a.py": "count = 2\n"},
        touched_files={"src/a.py"},
        out_of_scope_policy="suppress",
    )
    partial = core.finalize_review(
        [claim],
        change_summary={},
        fully_preloaded_paths=set(),
        file_contents={"src/a.py": "count = 2\n"},
        touched_files={"src/a.py"},
        out_of_scope_policy="suppress",
    )
    assert complete.findings == [claim]
    assert partial.findings == []
    assert partial.dropped["unverified_absence"][0]["preload_missing_files"] == [
        "src/a.py"
    ]


def test_soundness_annotation_does_not_mutate_input():
    invalid = {
        **finding(),
        "body": "This may look risky. On second thought, this is not a bug.",
    }
    before = deepcopy(invalid)
    result = finalize([invalid], out_of_scope_policy="suppress")
    assert result.findings == []
    assert result.dropped["self_disclaiming"][0]["disclaim_phrase"]
    assert invalid == before


@pytest.mark.parametrize("policy,score", [("suppress", None), ("informational", 5)])
def test_pr_level_findings_follow_existing_out_of_scope_policy(policy, score):
    """A finding with no file is out-of-scope in BOTH existing adapters.

    ``drop_out_of_scope_findings`` routes an empty ``file`` to out-of-scope
    (its SUPPRESSION_REASONS note names "an observation about the PR as a
    whole"), and both the sweep and the local artifact call it before scoring
    (``ai-review.py`` and ``ai-review-local.py``). This test pins that
    characterized behaviour so the shared API cannot drift from it, and so the
    disposition is explicit rather than an accident of ``"" not in
    touched_files``.
    """
    observation = finding(file="")
    result = finalize([observation], out_of_scope_policy=policy)
    assert result.findings == []
    assert result.out_of_scope_findings == [observation]
    assert result.score == score
    assert not result.blocking


@pytest.mark.parametrize("findings", [[], [finding()]])
def test_non_coverage_is_review_error_even_when_findings_exist(findings):
    with pytest.raises(core.ReviewError, match="could not read the input"):
        core.finalize_review(
            findings,
            change_summary={
                "summary": "The supplied diff cannot be reviewed because it is elided."
            },
            fully_preloaded_paths={"a.py"},
            file_contents={"a.py": "count = 2\n"},
            touched_files={"a.py"},
            out_of_scope_policy="informational",
        )


def test_submodule_abstention_is_not_clean_review():
    result = finalize([], out_of_scope_policy="informational", submodule_only=True)
    assert result.score is None
    assert not result.blocking


def test_invalid_scope_policy_cannot_silently_grant_clean_verdict():
    with pytest.raises(ValueError, match="out_of_scope_policy"):
        finalize([finding("untouched.py")], out_of_scope_policy="typo")
