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


def test_fetch_greptile_status_paginates_review_threads() -> None:
    first_page = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviews": {
                        "nodes": [
                            {
                                "author": {"login": "greptile-apps"},
                                "submittedAt": "2026-03-19T12:00:00Z",
                                "state": "COMMENTED",
                            }
                        ]
                    },
                    "reviewThreads": {
                        "pageInfo": {
                            "hasNextPage": True,
                            "endCursor": "cursor-1",
                        },
                        "nodes": [
                            {
                                "isResolved": True,
                                "comments": {
                                    "nodes": [
                                        {
                                            "author": {"login": "greptile-apps"},
                                            "createdAt": "2026-03-19T12:01:00Z",
                                        }
                                    ]
                                },
                            }
                        ],
                    },
                }
            }
        }
    }
    second_page = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviews": {
                        "nodes": [
                            {
                                "author": {"login": "greptile-apps"},
                                "submittedAt": "2026-03-19T12:00:00Z",
                                "state": "COMMENTED",
                            }
                        ]
                    },
                    "reviewThreads": {
                        "pageInfo": {
                            "hasNextPage": False,
                            "endCursor": None,
                        },
                        "nodes": [
                            {
                                "isResolved": False,
                                "comments": {
                                    "nodes": [
                                        {
                                            "author": {"login": "greptile-apps"},
                                            "createdAt": "2026-03-19T12:02:00Z",
                                        }
                                    ]
                                },
                            }
                        ],
                    },
                }
            }
        }
    }

    with patch.object(
        self_merge_check,
        "run_gh",
        side_effect=[
            self_merge_check.json.dumps(first_page),
            self_merge_check.json.dumps(second_page),
        ],
    ) as mock_run_gh:
        result = self_merge_check.fetch_greptile_status("gptme/gptme-contrib", 504)

    assert result == {"has_review": True, "unresolved": 1, "total": 2}
    assert mock_run_gh.call_count == 2
    first_query = mock_run_gh.call_args_list[0].args[0][-1]
    second_query = mock_run_gh.call_args_list[1].args[0][-1]
    assert "reviewThreads(first:100)" in first_query
    assert 'after:"cursor-1"' in second_query
