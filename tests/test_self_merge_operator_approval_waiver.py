"""Erik's APPROVED review on the exact head waives the AI-review requirement.

ErikBjare/bob#1471: Erik approves PRs instead of clicking merge. During a
reviewer outage the 2-pass consensus requirement could not be met, so
gptme/gptme-contrib#2093 stayed blocked despite an approval on its head.
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
spec = importlib.util.spec_from_file_location("self_merge_check_opapprove", MODULE_PATH)
if spec is None or spec.loader is None:
    pytest.skip(f"Could not load module from {MODULE_PATH}", allow_module_level=True)
smc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smc
spec.loader.exec_module(smc)

HEAD = "863e63683458a25f31b5b9e046c0d226614e4a29"
OLD = "c9995801ca000000000000000000000000000000"
ERIK = {"login": "ErikBjare", "user_id": 1405370, "type": "User"}


def _review(state: str, commit: str, **who: Any) -> dict[str, Any]:
    return {"id": 1, "state": state, "commit_id": commit, **{**ERIK, **who}}


def _waiver(reviews: list[dict[str, Any]] | None) -> str | None:
    raw = None if reviews is None else "\n".join(json.dumps(r) for r in reviews)
    with patch.object(smc, "run_gh_checked", return_value=raw):
        warning: str | None = smc.operator_approval_ai_review_waiver(
            repo="gptme/gptme-contrib", number=2093, head_sha=HEAD
        )
    return warning


def test_approval_on_head_waives() -> None:
    warning = _waiver([_review("APPROVED", HEAD)])
    assert warning is not None and HEAD[:12] in warning


def test_approval_on_older_head_does_not_waive() -> None:
    assert _waiver([_review("APPROVED", OLD)]) is None


def test_later_changes_requested_revokes() -> None:
    assert (
        _waiver([_review("APPROVED", HEAD), _review("CHANGES_REQUESTED", HEAD)]) is None
    )


def test_later_comment_does_not_revoke() -> None:
    assert _waiver([_review("APPROVED", HEAD), _review("COMMENTED", HEAD)]) is not None


def test_dismissed_approval_does_not_waive() -> None:
    assert _waiver([_review("APPROVED", HEAD), _review("DISMISSED", HEAD)]) is None


def test_non_operator_approval_does_not_waive() -> None:
    assert _waiver([_review("APPROVED", HEAD, login="someone", user_id=42)]) is None


def test_spoofed_login_with_wrong_id_does_not_waive() -> None:
    assert _waiver([_review("APPROVED", HEAD, user_id=999)]) is None


def test_bot_account_does_not_waive() -> None:
    assert _waiver([_review("APPROVED", HEAD, type="Bot")]) is None


def test_api_failure_fails_closed() -> None:
    assert _waiver(None) is None


def test_malformed_output_fails_closed() -> None:
    with patch.object(smc, "run_gh_checked", return_value="not json"):
        assert (
            smc.operator_approval_ai_review_waiver(
                repo="gptme/gptme-contrib", number=2093, head_sha=HEAD
            )
            is None
        )


@pytest.mark.parametrize("thread_state", ["unreadable", "clear", "unresolved"])
def test_operator_approval_preserves_human_thread_gate(
    monkeypatch: pytest.MonkeyPatch, thread_state: str
) -> None:
    """REST approval cannot turn unknown GraphQL thread state into permission."""
    monkeypatch.setattr(smc, "get_gh_user", lambda: "TimeToBuildBob")
    monkeypatch.setattr(smc, "merge_permission", lambda repo: True)
    monkeypatch.setattr(
        smc,
        "fetch_pr",
        lambda *a: {
            "number": 2098,
            "title": "docs: explain review policy",
            "url": "https://github.com/gptme/gptme-contrib/pull/2098",
            "author": {"login": "TimeToBuildBob"},
            "headRefOid": HEAD,
            "isDraft": False,
            "state": "OPEN",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
            "reviewDecision": None,
            "files": [{"path": "docs/review-tools.md"}],
            "labels": [],
        },
    )
    monkeypatch.setattr(
        smc,
        "_fetch_contrib_pr_file_shapes",
        lambda *a: [
            {"path": "docs/review-tools.md", "status": "modified", "patch": "+Policy"}
        ],
    )
    threads = (
        []
        if thread_state != "unresolved"
        else [
            {
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {"author": {"login": "ErikBjare"}, "body": "Please fix this"}
                    ]
                },
            }
        ]
    )
    monkeypatch.setattr(
        smc,
        "_fetch_greptile_review_data",
        lambda *a: None if thread_state == "unreadable" else ([], threads),
    )
    monkeypatch.setattr(
        smc, "_fetch_ai_review_marker_checked", lambda *a, **k: (None, True)
    )
    monkeypatch.setattr(
        smc,
        "fetch_ai_review_status",
        lambda *a, **k: {"accepted": False, "detail": "no consensus review"},
    )
    monkeypatch.setattr(smc, "run_gh", lambda *a, **k: "")
    monkeypatch.setattr(
        smc, "run_gh_checked", lambda *a, **k: json.dumps(_review("APPROVED", HEAD))
    )
    result = smc.evaluate_pr(
        "gptme/gptme-contrib", 2098, workspace_repos=["gptme/gptme-contrib"]
    )
    assert result.eligible is (thread_state == "clear"), result.reasons
    if thread_state == "unreadable":
        assert any("AI review required" in reason for reason in result.reasons)
    elif thread_state == "unresolved":
        assert any(
            "unresolved human review thread" in reason for reason in result.reasons
        )
