"""Erik's approval survives review-fix commits (ErikBjare/bob#1471 rule 3).

An APPROVED review on an earlier commit still waives path/category policy when
every later commit is a small, Bob-authored, non-merge change that touches only
files the PR already changed at the approved commit. The AI-review waiver stays
head-bound, so the new head still needs its own review.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "github"
    / "self-merge-check.py"
)
spec = importlib.util.spec_from_file_location(
    "self_merge_check_opapprove_fix_only", MODULE_PATH
)
if spec is None or spec.loader is None:
    pytest.skip(f"Could not load module from {MODULE_PATH}", allow_module_level=True)
smc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smc
spec.loader.exec_module(smc)

REPO = "gptme/gptme-contrib"
HEAD = "863e63683458a25f31b5b9e046c0d226614e4a29"
APPROVED = "c9995801ca000000000000000000000000000000"
BOB = "TimeToBuildBob"
ERIK = {"login": "ErikBjare", "user_id": 1405370, "type": "User"}
CATEGORY = "Files outside allowed categories: scripts/new-thing.py"
SENSITIVE = "Touches sensitive/security/infra paths"


def _review(state: str, commit: str, **who: Any) -> dict[str, Any]:
    return {"id": 1, "state": state, "commit_id": commit, **{**ERIK, **who}}


def _delta(
    *,
    status: str = "ahead",
    behind_by: int = 0,
    commits: list[dict[str, Any]] | None = None,
    files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "behind_by": behind_by,
        "commits": [{"author": BOB, "parents": 1}] if commits is None else commits,
        "files": [{"filename": "scripts/new-thing.py", "changes": 12}]
        if files is None
        else files,
    }


APPROVED_DIFF = {
    "status": "ahead",
    "behind_by": 0,
    "commits": [],
    "files": [
        {"filename": "scripts/new-thing.py", "changes": 80},
        {"filename": "tests/test_new_thing.py", "changes": 40},
    ],
}


def _fake_gh(reviews: list[dict[str, Any]] | None, delta: dict[str, Any] | None):
    def run(args: list[str], timeout: int = 30) -> str | None:
        path = args[1]
        if path.endswith("/reviews"):
            if reviews is None:
                return None
            return "\n".join(json.dumps(r) for r in reviews)
        if path == f"repos/{REPO}/compare/{APPROVED}...{HEAD}":
            return None if delta is None else json.dumps(delta)
        if path == f"repos/{REPO}/compare/master...{APPROVED}":
            return json.dumps(APPROVED_DIFF)
        raise AssertionError(f"unexpected gh call: {args}")

    return run


def _waive(
    reviews: list[dict[str, Any]] | None,
    delta: dict[str, Any] | None = None,
    reasons: list[str] | None = None,
) -> tuple[list[str], str | None]:
    with patch.object(smc, "run_gh_checked", side_effect=_fake_gh(reviews, delta)):
        result: tuple[list[str], str | None] = smc.operator_approval_category_waiver(
            [CATEGORY] if reasons is None else reasons,
            repo=REPO,
            number=2093,
            head_sha=HEAD,
            base_ref="master",
            author=BOB,
        )
    return result


def test_fix_only_commits_after_approval_waive_category() -> None:
    remaining, warning = _waive([_review("APPROVED", APPROVED)], _delta())
    assert remaining == []
    assert warning is not None
    assert APPROVED[:12] in warning and "1 fix-only commit(s)" in warning


def test_sensitive_stop_survives_fix_only_approval() -> None:
    remaining, warning = _waive(
        [_review("APPROVED", APPROVED)], _delta(), reasons=[SENSITIVE, CATEGORY]
    )
    assert remaining == [SENSITIVE]
    assert warning is not None


def test_fix_touching_a_file_outside_the_approved_diff_is_not_waived() -> None:
    delta = _delta(files=[{"filename": "scripts/unrelated.py", "changes": 3}])
    assert _waive([_review("APPROVED", APPROVED)], delta) == ([CATEGORY], None)


def test_large_delta_is_a_redesign_not_a_fix() -> None:
    delta = _delta(files=[{"filename": "scripts/new-thing.py", "changes": 151}])
    assert _waive([_review("APPROVED", APPROVED)], delta) == ([CATEGORY], None)


def test_commit_by_someone_else_is_not_waived() -> None:
    delta = _delta(
        commits=[{"author": BOB, "parents": 1}, {"author": "x", "parents": 1}]
    )
    assert _waive([_review("APPROVED", APPROVED)], delta) == ([CATEGORY], None)


def test_unattributed_commit_is_not_waived() -> None:
    delta = _delta(commits=[{"author": None, "parents": 1}])
    assert _waive([_review("APPROVED", APPROVED)], delta) == ([CATEGORY], None)


def test_merge_commit_since_approval_is_not_waived() -> None:
    delta = _delta(commits=[{"author": BOB, "parents": 2}])
    assert _waive([_review("APPROVED", APPROVED)], delta) == ([CATEGORY], None)


def test_rewritten_history_is_not_waived() -> None:
    delta = _delta(status="diverged", behind_by=2)
    assert _waive([_review("APPROVED", APPROVED)], delta) == ([CATEGORY], None)


def test_later_changes_requested_revokes_fix_only_approval() -> None:
    reviews = [_review("APPROVED", APPROVED), _review("CHANGES_REQUESTED", APPROVED)]
    assert _waive(reviews, _delta()) == ([CATEGORY], None)


def test_non_operator_approval_is_not_waived() -> None:
    reviews = [_review("APPROVED", APPROVED, login="someone", user_id=42)]
    assert _waive(reviews, _delta()) == ([CATEGORY], None)


def test_compare_failure_fails_closed() -> None:
    assert _waive([_review("APPROVED", APPROVED)], None) == ([CATEGORY], None)


def test_ai_review_waiver_stays_head_bound() -> None:
    with patch.object(
        smc,
        "run_gh_checked",
        side_effect=_fake_gh([_review("APPROVED", APPROVED)], _delta()),
    ):
        assert (
            smc.operator_approval_ai_review_waiver(
                repo=REPO, number=2093, head_sha=HEAD
            )
            is None
        )
