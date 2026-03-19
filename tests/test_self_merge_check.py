from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "github"
    / "self-merge-check.py"
)
spec = importlib.util.spec_from_file_location("self_merge_check", MODULE_PATH)
assert spec and spec.loader
self_merge_check = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = self_merge_check
spec.loader.exec_module(self_merge_check)


def test_checks_green_rejects_indeterminate_check() -> None:
    assert not self_merge_check.checks_green([{"status": None, "conclusion": None}])


def test_checks_green_allows_empty_list() -> None:
    assert self_merge_check.checks_green([])


def test_evaluate_pr_blocks_changes_requested() -> None:
    pr_data = {
        "author": {"login": "TimeToBuildBob"},
        "title": "Test PR",
        "url": "https://github.com/gptme/gptme-contrib/pull/999",
        "files": [{"path": "tests/test_example.py"}],
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "isDraft": False,
        "state": "OPEN",
        "reviewDecision": "CHANGES_REQUESTED",
    }

    with (
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": True, "unresolved": 0, "total": 1},
        ),
    ):
        result = self_merge_check.evaluate_pr(
            "gptme/gptme-contrib",
            999,
            workspace_repo="gptme/gptme-contrib",
        )

    assert not result.eligible
    assert "Review decision: CHANGES_REQUESTED" in result.reasons
    assert not result.warnings
