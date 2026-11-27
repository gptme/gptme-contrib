"""Epic slices get the placement/category waiver (Erik, 2026-11-24).

A PR that declares ``Epic: owner/repo#N`` for an open issue listed in
merge_policy.toml ``[epics]`` drops only the allowlisted placement reasons;
every other refusal, and every non-category gate, still applies.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        pytest.skip(f"Could not load {path}", allow_module_level=True)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


smc = _load("self_merge_check_epic_waiver", ROOT / "scripts/github/self-merge-check.py")
mp = smc.merge_policy

REPO = "gptme/gptme-contrib"
EPIC = "gptme/gptme-contrib#2310"
PLACEMENT = "New packages/<name>/ (gptme-foo): a brand-new package ..."
REQUIRED_CONFIG = "New required config key (gptme/config/x.py): Erik's call"
SENSITIVE = "Touches sensitive/security/infra paths"
BODY = f"Third slice.\n\nEpic: {EPIC}\n"


def _policy(issues=(EPIC,), repos=(REPO, "gptme/gptme")) -> dict:
    base = mp.load_policy(mp.POLICY_PATH)
    return {**base, "epics": {"issues": list(issues), "repos": list(repos)}}


def _issue(state: str = "open", is_pr: bool = False) -> str:
    return json.dumps({"state": state, "is_pr": is_pr})


def _waive(reasons, *, body=BODY, repo=REPO, policy=None, issue=_issue()):
    with (
        patch.object(mp, "load_policy", return_value=policy or _policy()),
        patch.object(smc, "run_gh_checked", return_value=issue) as gh,
    ):
        return smc.epic_category_waiver(reasons, repo=repo, body=body), gh


def test_declared_listed_open_epic_waives_placement_only() -> None:
    (remaining, warning), _ = _waive([PLACEMENT, REQUIRED_CONFIG, SENSITIVE])
    assert remaining == [REQUIRED_CONFIG, SENSITIVE]
    assert warning is not None and EPIC in warning


def test_nothing_waivable_reads_nothing() -> None:
    (remaining, warning), gh = _waive([REQUIRED_CONFIG, SENSITIVE])
    assert remaining == [REQUIRED_CONFIG, SENSITIVE]
    assert warning is None
    gh.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        "Part of the #2310 work, but unrelated: gptme/gptme-contrib#2310",
        "epic gptme/gptme-contrib#2310",
        "Epic: gptme/gptme-contrib#2310 and more",
        "",
    ],
)
def test_mention_without_declaration_line_is_not_a_claim(body: str) -> None:
    (remaining, warning), _ = _waive([PLACEMENT], body=body)
    assert remaining == [PLACEMENT]
    assert warning is None


@pytest.mark.parametrize(
    "line",
    [
        "Epic: gptme/gptme-contrib#2310",
        "epic:   #2310.",
        "Epic: https://github.com/gptme/gptme-contrib/issues/2310",
        "  EPIC: GPTME/gptme-contrib#2310",
    ],
)
def test_declaration_forms(line: str) -> None:
    assert smc.declared_epics(f"intro\n{line}\nmore", REPO) == [EPIC]


def test_same_repo_short_ref_resolves_against_pr_repo() -> None:
    assert smc.declared_epics("Epic: #2310", "gptme/gptme") == ["gptme/gptme#2310"]


def test_crlf_body_declaration_is_read() -> None:
    assert smc.declared_epics(f"intro\r\nEpic: {EPIC}\r\nmore\r\n", REPO) == [EPIC]


def test_epic_whose_repo_is_outside_epic_repos_is_not_waived() -> None:
    # A listed epic still needs its own repo in [epics].repos, not just the PR's.
    (remaining, warning), _ = _waive(
        [PLACEMENT],
        repo="gptme/gptme",
        body=f"Epic: {EPIC}",
        policy=_policy(repos=("gptme/gptme",)),
    )
    assert remaining == [PLACEMENT]
    assert warning is None


def test_unlisted_epic_is_not_waived() -> None:
    (remaining, warning), _ = _waive([PLACEMENT], body="Epic: gptme/gptme-contrib#1")
    assert remaining == [PLACEMENT]
    assert warning is None


def test_repo_outside_epic_repos_is_not_waived() -> None:
    (remaining, warning), _ = _waive(
        ["Files not in any allowed self-merge category: src/a.ts"],
        repo="ActivityWatch/aw-webui",
        body=f"Epic: {EPIC}",
    )
    assert warning is None
    assert remaining == ["Files not in any allowed self-merge category: src/a.ts"]


@pytest.mark.parametrize(
    "issue",
    [_issue(state="closed"), _issue(is_pr=True), None, "not json", "[]"],
)
def test_closed_pr_or_unreadable_epic_fails_closed(issue) -> None:
    (remaining, warning), _ = _waive([PLACEMENT], issue=issue)
    assert remaining == [PLACEMENT]
    assert warning is None


def test_untrusted_policy_fails_closed() -> None:
    with patch.object(mp, "load_policy", side_effect=ValueError("differs")):
        remaining, warning = smc.epic_category_waiver([PLACEMENT], repo=REPO, body=BODY)
    assert remaining == [PLACEMENT]
    assert warning is None


@pytest.mark.parametrize(
    "reason",
    [
        "New CLI surface (gptme/cli/x.py): ...",
        "Writes under state/ (state/x.json): ...",
        "Agent-name literal in non-test code (bob): ...",
        "Feature is 900 counted lines (> 800, tests excluded)",
        "Bot/CI config changes require human review under current policy",
        "Touches spec-like documentation requiring human review",
    ],
)
def test_held_reasons_survive_an_epic(reason: str) -> None:
    (remaining, warning), _ = _waive([reason])
    assert remaining == [reason]
    assert warning is None


def test_shipped_policy_lists_the_first_epic() -> None:
    policy = mp.load_policy(mp.POLICY_PATH)
    assert EPIC in mp.epic_issues(policy)
    assert mp.epic_repos(policy) == {"gptme/gptme", "gptme/gptme-contrib"}
    assert not any(r.startswith("activitywatch/") for r in mp.epic_repos(policy))


def _stub_clean_pr(monkeypatch, *, agent_literal_reasons=()) -> None:
    """Stub evaluate_pr down to the category/waiver stage for a contrib PR."""
    monkeypatch.setattr(
        smc,
        "fetch_pr",
        lambda *a: {
            "author": {"login": "TimeToBuildBob"},
            "body": BODY,
            "files": [{"path": "packages/gptme-foo/src/gptme_foo/x.py"}],
            "headRefOid": "abc",
            "state": "OPEN",
            "labels": [],
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        },
    )
    monkeypatch.setattr(smc, "get_gh_user", lambda: "TimeToBuildBob")
    monkeypatch.setattr(smc, "merge_permission", lambda *a: True)
    monkeypatch.setattr(
        smc,
        "fetch_greptile_status",
        lambda *a, **k: {"has_review": False, "unresolved": 0},
    )
    monkeypatch.setattr(smc, "_fetch_greptile_review_data", lambda *a: ([], []))
    monkeypatch.setattr(
        smc,
        "fetch_unresolved_human_threads",
        lambda *a, **k: {"unresolved": 0, "authors": []},
    )
    monkeypatch.setenv("SELF_MERGE_ACCEPT_AI_REVIEW", "0")
    monkeypatch.setattr(
        smc, "_fetch_ai_review_marker_checked", lambda *a, **k: (None, True)
    )
    monkeypatch.setattr(
        smc,
        "fetch_ai_review_status",
        lambda *a, **k: {"accepted": False, "detail": "no review"},
    )
    monkeypatch.setattr(
        smc, "contrib_classify_category", lambda *a, **k: (None, [PLACEMENT])
    )
    monkeypatch.setattr(smc, "merge_policy_tier_waiver", lambda r, **k: (r, None, None))
    monkeypatch.setattr(
        smc, "operator_approval_category_waiver", lambda r, **k: (r, None)
    )
    monkeypatch.setattr(smc, "_fetch_contrib_pr_file_shapes", lambda *a: [])
    monkeypatch.setattr(
        smc, "contrib_agent_literal_reasons", lambda *a: list(agent_literal_reasons)
    )


def test_evaluate_pr_passes_body_and_marks_epic_slice(monkeypatch) -> None:
    _stub_clean_pr(monkeypatch)
    seen: dict = {}

    def fake_epic(reasons, *, repo, body):
        seen.update(reasons=list(reasons), repo=repo, body=body)
        return [], "epic slice"

    monkeypatch.setattr(smc, "epic_category_waiver", fake_epic)
    result = smc.evaluate_pr(REPO, 1, workspace_repos=[REPO])
    assert seen == {"reasons": [PLACEMENT], "repo": REPO, "body": BODY}
    assert result.category == "epic-slice"
    assert "epic slice" in result.warnings
    assert PLACEMENT not in result.reasons


def test_epic_waiver_cannot_skip_the_agent_literal_hold(monkeypatch) -> None:
    """The placement refusal that an epic clears is also what keeps a contrib
    PR out of the agent-literal detector. Clearing it must not drop that hold."""
    hold = "Agent-name literal ('bob_config') in non-test code routes to Erik"
    _stub_clean_pr(monkeypatch, agent_literal_reasons=[hold])
    monkeypatch.setattr(smc, "epic_category_waiver", lambda r, **k: ([], "epic slice"))

    result = smc.evaluate_pr(REPO, 1, workspace_repos=[REPO])

    assert hold in result.reasons
    assert result.category is None
    assert not result.eligible
