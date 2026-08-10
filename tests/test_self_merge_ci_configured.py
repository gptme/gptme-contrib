"""`repo_gates_prs_with_checks` must fail closed when it cannot determine the answer.

This guard exists to stop a self-merge when a repo *does* gate PRs with checks
but the checks produced no result — the shape seen during the 2026-08-06 GitHub
Actions outage, where `statusCheckRollup` came back empty on repos that very
much do run CI (`ErikBjare/bob#1130` is in that window). Only a definitive "this
repo does not gate PRs" may waive the CI requirement; anything indeterminate
fails closed.

Two review findings on gptme/gptme-contrib#1382 (see ErikBjare/bob#1122) pushed
the probe from "does this repo have Actions workflows" to "do this repo's PRs
report checks", because the workflow-presence question fails in *both*
directions and the two fixes pull against each other:

* Counting active Actions workflows says "no CI" for any repo gated by Jenkins,
  Azure Pipelines, Travis, or a GitHub App status context — waiving the
  requirement and reopening the outage fail-open for non-Actions CI.
* It says "has CI" for a repo whose only workflows are push/schedule-triggered,
  which produces no PR checks by design — making every PR on such a repo
  permanently self-merge-ineligible. That trades a fail-open for a permanent
  block, which is worse.

Broadening the predicate fixes the first and worsens the second; narrowing it to
PR-triggered workflows does the reverse. Sampling *observed PR check history*
answers the question the gate actually asks and resolves both at once:
`statusCheckRollup` carries `StatusContext` entries as well as `CheckRun` ones,
so external CI counts, while a push-only repo's PRs are correctly seen to carry
no checks.

A follow-up review found the remaining hole: a *silent* sample (every sampled PR
reported zero checks) has an innocent cause and a dangerous one, and the sample
cannot tell them apart — the repo may genuinely not gate PRs, or the sample may
be unrepresentative because CI was added after those merges or every sampled
merge came from a fork whose workflows were never approved. So `False` is
returned only when `repo_has_pull_request_workflow_runs` corroborates it;
otherwise the answer is `None` and the caller fails closed. Counting
pull_request-event *runs* is safe where counting *workflows* was not, because a
push-only repo has workflows but zero PR runs. The sample is also scoped to the
PR's base branch, since CI is often branch-specific.

The tests below pin every direction against real-world shapes:
`ErikBjare/erikbjare.github.io` (2 active workflows, 0 checks on all 4 merged
PRs, 0 pull_request runs) is the push-only control; a Jenkins-style
`StatusContext` payload is the external-CI control; `gptme/gptme-contrib`
(13664 pull_request runs) and `ErikBjare/bob` (1151) are the gated controls.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "github" / "self-merge-check.py"
)

# Register in sys.modules before exec_module — the script defines @dataclass at
# import time, and dataclasses resolves annotations via sys.modules[__module__].
spec = importlib.util.spec_from_file_location("self_merge_check", SCRIPT)
if spec is None or spec.loader is None:
    pytest.skip(f"Could not load module from {SCRIPT}", allow_module_level=True)
smc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smc
spec.loader.exec_module(smc)


class _Proc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _pr_list(*check_counts: int) -> str:
    """A `gh pr list --json number,statusCheckRollup` payload.

    Each argument is the number of `CheckRun` entries on one merged PR.
    """
    return json.dumps(
        [
            {
                "number": 100 + i,
                "statusCheckRollup": [
                    {
                        "__typename": "CheckRun",
                        "name": f"job-{j}",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                    }
                    for j in range(count)
                ],
            }
            for i, count in enumerate(check_counts)
        ]
    )


# A repo gated by Jenkins / Azure / Travis / any GitHub App reports legacy
# commit statuses, which arrive as StatusContext rather than CheckRun.
_EXTERNAL_CI_PR_LIST = json.dumps(
    [
        {
            "number": 7,
            "statusCheckRollup": [
                {
                    "__typename": "StatusContext",
                    "context": "continuous-integration/jenkins/pr-merge",
                    "state": "SUCCESS",
                }
            ],
        }
    ]
)


def _route(pr_list: str, pr_runs: str = "0", pr_list_rc: int = 0):
    """Dispatch a fake `subprocess.run` on argv.

    The probe makes up to two different calls — `gh pr list` for the history
    sample and `gh api .../actions/runs?event=pull_request` to corroborate a
    silent one — so a single canned response cannot stand in for both.
    """

    def _run(args: list[str], **kwargs: object) -> _Proc:
        if "runs?event=pull_request" in " ".join(args):
            return _Proc(0, pr_runs)
        return _Proc(pr_list_rc, pr_list if pr_list_rc == 0 else "")

    return _run


@pytest.mark.parametrize(
    ("label", "proc", "expected"),
    [
        ("every sampled PR reported checks", _Proc(0, _pr_list(16, 13, 16)), True),
        ("one sampled PR reported checks", _Proc(0, _pr_list(0, 0, 4, 0)), True),
        ("external CI via StatusContext", _Proc(0, _EXTERNAL_CI_PR_LIST), True),
        # Everything below is indeterminate and MUST be None, not False.
        ("no merged PRs to judge from", _Proc(0, "[]"), None),
        ("404 from the API", _Proc(1, "", "gh: Not Found (HTTP 404)"), None),
        ("rate limited", _Proc(1, "", "API rate limit exceeded"), None),
        ("server error", _Proc(1, "", "HTTP 502 Bad Gateway"), None),
        ("auth failure", _Proc(1, "", "HTTP 401 Bad credentials"), None),
        ("empty stdout on success", _Proc(0, ""), None),
        ("unparseable stdout", _Proc(0, "not json"), None),
        ("null payload", _Proc(0, "null"), None),
        ("object instead of list", _Proc(0, '{"message":"Not Found"}'), None),
        ("list of non-objects", _Proc(0, "[1, 2, 3]"), None),
    ],
)
def test_tri_state(
    monkeypatch: pytest.MonkeyPatch, label: str, proc: _Proc, expected: bool | None
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: proc)
    assert smc.repo_gates_prs_with_checks("o/r") is expected, label


_NOT_FOUND = _Proc(1, "", "gh: Not Found (HTTP 404)")


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired("gh", 30), OSError("boom")])
def test_exceptions_are_indeterminate(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """A timeout is the single likeliest symptom of the outage we guard against."""

    def _raise(*a: object, **k: object) -> None:
        raise exc

    monkeypatch.setattr(subprocess, "run", _raise)
    assert smc.repo_gates_prs_with_checks("o/r") is None


def test_probe_samples_merged_pr_check_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the query shape: merged PRs, with their check rollup, bounded."""
    captured: list[list[str]] = []

    def _capture(args: list[str], **kwargs: object) -> _Proc:
        captured.append(list(args))
        return _Proc(0, _pr_list(2))

    monkeypatch.setattr(subprocess, "run", _capture)
    assert smc.repo_gates_prs_with_checks("o/r") is True

    assert captured, "gh was never invoked"
    argv = captured[0]
    assert argv[:3] == ["gh", "pr", "list"], argv
    assert "--repo" in argv and argv[argv.index("--repo") + 1] == "o/r"
    assert argv[argv.index("--state") + 1] == "merged"
    assert argv[argv.index("--limit") + 1] == str(smc.PR_CHECK_HISTORY_SAMPLE)
    assert "statusCheckRollup" in argv[argv.index("--json") + 1]


def test_push_only_workflow_repo_is_not_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 regression: active workflows that never run on PRs must not block.

    Real-world control: ``ErikBjare/erikbjare.github.io`` has two active Actions
    workflows and zero checks on every one of its merged PRs (a deploy-on-push
    job). A workflow-presence probe calls that "has CI" and disqualifies every
    PR on the repo forever. History says "these PRs are not gated", which is the
    truth, so the waiver applies.
    """
    monkeypatch.setattr(subprocess, "run", _route(_pr_list(0, 0, 0, 0), pr_runs="0"))
    assert smc.repo_gates_prs_with_checks("ErikBjare/erikbjare.github.io") is False

    result = _evaluate_with(monkeypatch, False)
    assert result.eligible
    assert not any("anomalous" in r for r in result.reasons)


def test_silent_sample_is_indeterminate_when_pr_runs_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 regression: a silent sample is not proof the repo is ungated.

    The same "all sampled PRs reported zero checks" observation has an innocent
    cause (the repo really does not gate PRs) and a dangerous one (the sample is
    unrepresentative — CI added after those merges, or every sampled merge came
    from a fork whose workflows were never approved). Trusting it blindly would
    waive the CI requirement for a genuinely gated repo, recreating the
    fail-open this change exists to close.

    So a negative sample must be corroborated. Actions reporting *any*
    pull_request-event run means the sample is not the whole story → None →
    fail closed.
    """
    monkeypatch.setattr(subprocess, "run", _route(_pr_list(0, 0, 0), pr_runs="1151"))
    assert smc.repo_gates_prs_with_checks("o/r") is None


def test_silent_sample_with_unreadable_corroboration_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a definitive "no PR runs" may downgrade a silent sample to False."""

    def _run(args: list[str], **kwargs: object) -> _Proc:
        if "runs?event=pull_request" in " ".join(args):
            return _NOT_FOUND
        return _Proc(0, _pr_list(0, 0))

    monkeypatch.setattr(subprocess, "run", _run)
    assert smc.repo_gates_prs_with_checks("o/r") is None


def test_probe_scopes_sample_to_the_base_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI is often branch-specific, so the sample must be too.

    A repo that gates `master` but not `dev` would otherwise read as ungated
    whenever recent merge activity happened to be concentrated on `dev`.
    """
    captured: list[list[str]] = []

    def _capture(args: list[str], **kwargs: object) -> _Proc:
        captured.append(list(args))
        return _Proc(0, _pr_list(3))

    monkeypatch.setattr(subprocess, "run", _capture)
    assert smc.repo_gates_prs_with_checks("o/r", "master") is True
    assert captured[0][captured[0].index("--base") + 1] == "master"

    captured.clear()
    assert smc.repo_gates_prs_with_checks("o/r") is True
    assert "--base" not in captured[0], "unknown base must not be sent as a filter"


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [("13664", True), ("1", True), ("0", False), ("", None), ("nope", None)],
)
def test_repo_has_pull_request_workflow_runs(
    monkeypatch: pytest.MonkeyPatch, stdout: str, expected: bool | None
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(0, stdout))
    assert smc.repo_has_pull_request_workflow_runs("o/r") is expected


def test_external_ci_repo_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1 regression: non-Actions CI must still count as gating PRs.

    A repo whose checks come from Jenkins / Azure / Travis / a GitHub App has no
    Actions workflows at all, so a workflow-presence probe waives the CI
    requirement — the same outage fail-open this gate exists to close, just for
    non-Actions CI. ``statusCheckRollup`` reports those as ``StatusContext``, so
    sampling history sees them.
    """
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _Proc(0, _EXTERNAL_CI_PR_LIST)
    )
    assert smc.repo_gates_prs_with_checks("o/jenkins-repo") is True

    result = _evaluate_with(monkeypatch, True)
    assert not result.eligible
    assert any("anomalous" in r for r in result.reasons)


def _evaluate_with(
    monkeypatch: pytest.MonkeyPatch,
    gates_prs: bool | None = None,
    *,
    use_real_ci_probe: bool = False,
    is_cross_repository: bool = False,
    author_association: str = "MEMBER",
) -> Any:
    """Run evaluate_pr against a PR whose statusCheckRollup came back empty."""
    pr = {
        "number": 1,
        "title": "t",
        "url": "https://github.com/o/r/pull/1",
        "state": "OPEN",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [],
        "files": [{"path": "tests/test_example.py"}],
        "author": {"login": "TimeToBuildBob"},
        "reviews": [],
        "comments": [],
        "isCrossRepository": is_cross_repository,
        "baseRefName": "master",
    }
    monkeypatch.setattr(smc, "fetch_pr", lambda *a, **k: pr)
    real_run_gh = smc.run_gh
    if not use_real_ci_probe:
        monkeypatch.setattr(
            smc, "repo_gates_prs_with_checks", lambda *a, **k: gates_prs
        )
    greptile_review = {
        "author": {"login": "greptile-apps[bot]"},
        "submittedAt": "2026-08-09T00:00:00Z",
    }
    monkeypatch.setattr(
        smc, "_fetch_greptile_review_data", lambda *a, **k: ([greptile_review], [])
    )
    monkeypatch.setattr(smc, "greptile_summary_score", lambda *a, **k: 5)
    monkeypatch.setattr(smc, "get_gh_user", lambda: "TimeToBuildBob")

    # evaluate_pr reaches two more helpers that shell out to a real `gh`:
    #   * merge_permission — and it is @cache'd, so an unstubbed call poisons the
    #     cache for every later test in the session with a live network result.
    #     A definitive False would append a disqualifying reason and mask the
    #     CI-gate reasons these tests assert on.
    #   * run_gh — reached via fetch_greptile_status's summary-comment fallback,
    #     which fires because the stubbed review data is empty.
    # Stub both so the CI gate is the only thing under test. When the real CI
    # probe is under test it must still reach `subprocess.run` (which the caller
    # has monkeypatched), so its own `gh pr list` call is passed through.
    def _run_gh(args: list[str], **kwargs: Any) -> str:
        if use_real_ci_probe and args[:2] == ["pr", "list"]:
            return str(real_run_gh(args, **kwargs))
        # pr_author_association's REST lookup — gh pr view --json cannot supply it.
        if args[:2] == ["api", "repos/o/r/pulls/1"]:
            return author_association
        return ""

    monkeypatch.setattr(smc, "merge_permission", lambda repo: True)
    monkeypatch.setattr(smc, "run_gh", _run_gh)
    # `_fetch_ai_review_marker_checked` goes through `run_gh_checked`, which calls
    # `subprocess.run` directly rather than through `run_gh` — so stubbing `run_gh`
    # does not cover it. `evaluate_pr` fetches the marker unconditionally, so
    # without this the CI-gate tests shell out to a real `gh`. None means "fetch
    # succeeded, PR carries no marker"; the stubbed Greptile review above is what
    # satisfies the review gate, leaving CI as the only thing under test.
    monkeypatch.setattr(smc, "_fetch_ai_review_marker_checked", lambda *a, **k: None)
    return smc.evaluate_pr("o/r", 1, workspace_repos=["o/r"])


def test_api_error_blocks_via_real_evaluate_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the real helper wiring at the ``evaluate_pr`` call site."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _NOT_FOUND)
    result = _evaluate_with(monkeypatch, use_real_ci_probe=True)
    assert not result.eligible
    assert any("failing closed" in r for r in result.reasons)


def test_gating_history_blocks_via_real_evaluate_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end with the real probe: observed PR checks must disqualify.

    This is the ``ErikBjare/bob#1130`` outage shape — the repo's merged PRs
    report checks, this PR reports none.
    """
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _Proc(0, _pr_list(11, 24, 0))
    )
    result = _evaluate_with(monkeypatch, use_real_ci_probe=True)
    assert not result.eligible
    assert any("anomalous" in r for r in result.reasons)


def test_indeterminate_ci_blocks_self_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression: None must disqualify, not fall through to a warning.

    Without this, an API error during an outage re-enables the exact merge the
    guard was added to prevent.
    """
    result = _evaluate_with(monkeypatch, None)
    assert not result.eligible
    assert any("failing closed" in r for r in result.reasons)


def test_gated_repo_with_no_checks_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _evaluate_with(monkeypatch, True)
    assert not result.eligible
    assert any("anomalous" in r for r in result.reasons)


@pytest.mark.parametrize("association", sorted(smc.UNAPPROVED_WORKFLOW_ASSOCIATIONS))
def test_unapproved_fork_workflows_name_the_approval_button(
    monkeypatch: pytest.MonkeyPatch, association: str
) -> None:
    """The un-approved-workflow case must be reported as itself.

    Real-world control: gptme-contrib#1232 is a fork PR by a
    FIRST_TIME_CONTRIBUTOR that sat at zero check runs for 36 days because
    GitHub held its workflows behind "Approve and run workflows". The repo's
    `pre-commit.yml` fires on every PR to master with no path filter — nothing
    was missing but the button press. Reporting that as "no CI configured"
    sends the reader hunting for a workflow file that exists, and the un-run
    job would have caught a real defect (a stray `END` heredoc marker).
    """
    result = _evaluate_with(
        monkeypatch,
        True,
        is_cross_repository=True,
        author_association=association,
    )
    assert not result.eligible
    reason = " ".join(result.reasons)
    assert "Approve and run workflows" in reason
    assert "no workflow file is missing" in reason
    assert association in reason


def test_member_fork_gets_the_generic_anomaly_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fork provenance alone must not trigger the approval message.

    Control: gptme-contrib#1240 is *also* a fork PR, but its author is a
    MEMBER, so its workflows ran automatically (17 checks). Keying the message
    off `isCrossRepository` alone would misreport it.
    """
    result = _evaluate_with(
        monkeypatch, True, is_cross_repository=True, author_association="MEMBER"
    )
    assert not result.eligible
    reason = " ".join(result.reasons)
    assert "Approve and run workflows" not in reason
    assert "anomalous" in reason


def test_same_repo_first_timer_gets_the_generic_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Branch PRs are not held for approval, so the button is not the answer."""
    result = _evaluate_with(
        monkeypatch,
        True,
        is_cross_repository=False,
        author_association="FIRST_TIME_CONTRIBUTOR",
    )
    assert "Approve and run workflows" not in " ".join(result.reasons)


def test_generic_message_still_lists_pending_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stricter repo settings hold more associations, so keep it in the list."""
    result = _evaluate_with(monkeypatch, True, author_association="CONTRIBUTOR")
    assert any("pending maintainer approval" in r for r in result.reasons)


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("FIRST_TIME_CONTRIBUTOR\n", "FIRST_TIME_CONTRIBUTOR"),
        ("member", "MEMBER"),
        ("", ""),
    ],
)
def test_pr_author_association(
    monkeypatch: pytest.MonkeyPatch, stdout: str, expected: str
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(0, stdout))
    assert smc.pr_author_association("o/r", 1) == expected


def test_pr_author_association_is_unreadable_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An API failure must degrade to the generic message, not crash."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _NOT_FOUND)
    assert smc.pr_author_association("o/r", 1) == ""


def test_ungated_repo_is_waived(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repo whose PRs are not gated by checks still gets the waiver."""
    result = _evaluate_with(monkeypatch, False)
    assert result.eligible
    assert any("No CI gates PRs" in w for w in result.warnings)
    assert not any("CI checks not found" in r for r in result.reasons)
    assert not any("failing closed" in r for r in result.reasons)


class _NoNetwork(RuntimeError):
    """Raised if a test reaches a real subprocess. Not caught by the module.

    Deliberately not a subclass of anything ``self-merge-check.py`` catches
    (``TimeoutExpired`` / ``OSError`` / ``JSONDecodeError``), so an escape shows
    up as an error rather than being silently swallowed into an indeterminate
    result that happens to match the assertion.
    """


def test_evaluate_pr_is_hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_evaluate_with`` must not shell out to a real ``gh``.

    ``evaluate_pr`` reaches ``merge_permission`` and the ``fetch_greptile_status``
    summary-comment fallback, neither of which was stubbed. Both ran a real
    ``gh api`` against github.com, so these tests were network-dependent: they
    could hang on a slow API, flip on rate limits, or — worse — quietly change
    the assertion surface (a definitive "no merge permission" adds a
    disqualifying reason, which would mask exactly the CI-gate reasons under
    test).
    """

    def _explode(*a: object, **k: object) -> None:
        raise _NoNetwork(f"real subprocess invoked: {a!r}")

    monkeypatch.setattr(subprocess, "run", _explode)
    result = _evaluate_with(monkeypatch, False)
    assert any("No CI gates PRs" in w for w in result.warnings)
