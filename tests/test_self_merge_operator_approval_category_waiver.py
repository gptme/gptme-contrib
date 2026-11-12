"""Erik's APPROVED review on the exact head overrides path/category policy.

ErikBjare/bob#1471 phase 2. The sensitive-path and bot-config stops survive:
an approval is human review of the change, not an exemption for credential,
CI or bot-policy paths.
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
    "self_merge_check_opapprove_category", MODULE_PATH
)
if spec is None or spec.loader is None:
    pytest.skip(f"Could not load module from {MODULE_PATH}", allow_module_level=True)
smc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smc
spec.loader.exec_module(smc)

HEAD = "863e63683458a25f31b5b9e046c0d226614e4a29"
OLD = "c9995801ca000000000000000000000000000000"
ERIK = {"login": "ErikBjare", "user_id": 1405370, "type": "User"}
SENSITIVE = "Touches sensitive/security/infra paths"
BOT = "Bot/CI config changes require human review under current policy"
CATEGORY = "Files outside allowed categories: scripts/new-thing.py"
SPEC = "Touches spec-like documentation requiring human review"


def _review(state: str, commit: str, **who: Any) -> dict[str, Any]:
    return {"id": 1, "state": state, "commit_id": commit, **{**ERIK, **who}}


def _waive(
    reasons: list[str], reviews: list[dict[str, Any]] | None
) -> tuple[list[str], str | None, int]:
    raw = None if reviews is None else "\n".join(json.dumps(r) for r in reviews)
    with patch.object(smc, "run_gh_checked", return_value=raw) as gh:
        remaining, warning = smc.operator_approval_category_waiver(
            reasons, repo="gptme/gptme-contrib", number=2093, head_sha=HEAD
        )
    return remaining, warning, gh.call_count


def test_approval_on_head_waives_category_reasons() -> None:
    remaining, warning, _ = _waive([CATEGORY, SPEC], [_review("APPROVED", HEAD)])
    assert remaining == []
    assert warning is not None and HEAD[:12] in warning and "2 reason(s)" in warning


def test_sensitive_path_stop_survives_approval() -> None:
    remaining, warning, _ = _waive([SENSITIVE, CATEGORY], [_review("APPROVED", HEAD)])
    assert remaining == [SENSITIVE]
    assert warning is not None


def test_bot_config_stop_survives_approval() -> None:
    remaining, warning, _ = _waive([BOT], [_review("APPROVED", HEAD)])
    assert remaining == [BOT]
    assert warning is None


def test_hard_stops_only_do_not_read_reviews() -> None:
    remaining, warning, calls = _waive([SENSITIVE, BOT], [_review("APPROVED", HEAD)])
    assert (remaining, warning, calls) == ([SENSITIVE, BOT], None, 0)


def test_approval_on_older_head_does_not_waive() -> None:
    remaining, warning, _ = _waive([CATEGORY], [_review("APPROVED", OLD)])
    assert (remaining, warning) == ([CATEGORY], None)


def test_later_changes_requested_revokes() -> None:
    reviews = [_review("APPROVED", HEAD), _review("CHANGES_REQUESTED", HEAD)]
    assert _waive([CATEGORY], reviews)[:2] == ([CATEGORY], None)


def test_non_operator_approval_does_not_waive() -> None:
    reviews = [_review("APPROVED", HEAD, login="someone", user_id=42)]
    assert _waive([CATEGORY], reviews)[:2] == ([CATEGORY], None)


def test_api_failure_fails_closed() -> None:
    assert _waive([CATEGORY], None)[:2] == ([CATEGORY], None)


def test_ai_review_waiver_still_delegates_to_head_approval() -> None:
    raw = json.dumps(_review("APPROVED", HEAD))
    with patch.object(smc, "run_gh_checked", return_value=raw):
        warning = smc.operator_approval_ai_review_waiver(
            repo="gptme/gptme-contrib", number=2093, head_sha=HEAD
        )
    assert warning is not None and "AI review waived" in warning
