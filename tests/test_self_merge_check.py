from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "github"
    / "self-merge-check.py"
)
spec = importlib.util.spec_from_file_location("self_merge_check", MODULE_PATH)
if spec is None or spec.loader is None:
    pytest.skip(f"Could not load module from {MODULE_PATH}", allow_module_level=True)
self_merge_check = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = self_merge_check
spec.loader.exec_module(self_merge_check)


def test_checks_green_rejects_indeterminate_check() -> None:
    assert not self_merge_check.checks_green([{"status": None, "conclusion": None}])


def test_checks_green_rejects_completed_without_conclusion() -> None:
    # COMPLETED check with no conclusion is indeterminate — should not pass
    assert not self_merge_check.checks_green(
        [{"status": "COMPLETED", "conclusion": None}]
    )
    assert not self_merge_check.checks_green(
        [{"status": "COMPLETED", "conclusion": ""}]
    )


def test_checks_green_allows_empty_list() -> None:
    assert self_merge_check.checks_green([])


def test_parse_pr_target_rejects_malformed_url() -> None:
    """Malformed URL missing owner/repo path segments must raise ValueError."""
    import pytest

    with pytest.raises(ValueError, match="Not a PR URL"):
        self_merge_check.parse_pr_target("https://github.com/pull/123", None, None)


def test_parse_pr_target_accepts_valid_url() -> None:
    repo, number = self_merge_check.parse_pr_target(
        "https://github.com/gptme/gptme-contrib/pull/504", None, None
    )
    assert repo == "gptme/gptme-contrib"
    assert number == 504


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


def test_detect_workspace_repo_falls_back_when_cwd_remote_is_not_github() -> None:
    completed = self_merge_check.subprocess.CompletedProcess(
        args=["git"],
        returncode=0,
        stdout="git@gitlab.com:owner/repo.git\n",
        stderr="",
    )

    with (
        patch.object(self_merge_check.Path, "cwd", return_value=Path("/tmp/current")),
        patch.object(
            self_merge_check.Path,
            "exists",
            autospec=True,
            side_effect=lambda p: str(p)
            in {
                "/tmp/current/.git",
                str(MODULE_PATH.parent / ".git"),
            },
        ),
        patch.object(
            self_merge_check.subprocess, "run", return_value=completed
        ) as mock_run,
        patch.object(
            self_merge_check,
            "_parse_remote_url",
            side_effect=["", "gptme/gptme-contrib"],
        ),
    ):
        repo = self_merge_check.detect_workspace_repo()

    assert repo == "gptme/gptme-contrib"
    assert mock_run.call_count == 2


def test_fetch_pr_uses_paginated_rest_files_api() -> None:
    pr_metadata = {
        "number": 504,
        "title": "Test PR",
        "url": "https://github.com/gptme/gptme-contrib/pull/504",
        "author": {"login": "TimeToBuildBob"},
        "statusCheckRollup": [],
        "isDraft": False,
        "state": "OPEN",
        "reviewDecision": None,
    }
    files_output = "\n".join(
        [json.dumps({"path": f"tests/test_{i}.py"}) for i in range(105)]
    )

    with patch.object(
        self_merge_check,
        "run_gh",
        side_effect=[json.dumps(pr_metadata), files_output],
    ) as mock_run_gh:
        pr = self_merge_check.fetch_pr("gptme/gptme-contrib", 504)

    assert len(pr["files"]) == 105
    assert pr["files"][104]["path"] == "tests/test_104.py"
    first_call = mock_run_gh.call_args_list[0]
    second_call = mock_run_gh.call_args_list[1]
    assert "files" not in first_call.args[0][-1]
    assert second_call.args[0][:4] == [
        "api",
        "repos/gptme/gptme-contrib/pulls/504/files",
        "--paginate",
        "--jq",
    ]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("packages/docker-deployment.yaml", True),
        ("packages/service-deployer.py", True),
        ("packages/author_utils.py", False),
    ],
)
def test_is_sensitive_path_handles_deploy_word_forms(path: str, expected: bool) -> None:
    assert self_merge_check.is_sensitive_path(path) is expected


def test_evaluate_pr_warns_when_workspace_repo_empty() -> None:
    """Cross-repo restriction must emit a warning (not silently skip) when workspace repo is undetectable."""
    pr_data = {
        "author": {"login": "TimeToBuildBob"},
        "title": "Test PR",
        "url": "https://github.com/gptme/gptme-contrib/pull/999",
        "files": [{"path": "tests/test_example.py"}],
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "isDraft": False,
        "state": "OPEN",
        "reviewDecision": None,
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
            workspace_repo="",  # detection failure
        )

    # Cross-repo restriction cannot be enforced, so a warning must be emitted
    assert any("cross-repo restriction is disabled" in w for w in result.warnings)


def test_fetch_greptile_status_fallback_paginates_issue_comments() -> None:
    """Fallback path must use --paginate so Greptile's comment isn't missed on busy PRs."""
    # Simulate no formal Greptile review (GraphQL returns no greptile reviewer)
    graphql_page = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviews": {"nodes": []},
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [],
                    },
                }
            }
        }
    }
    # Fallback REST call returns a Greptile comment ID (one line of output)
    fallback_response = "123456789"

    with patch.object(
        self_merge_check,
        "run_gh",
        side_effect=[
            self_merge_check.json.dumps(graphql_page),
            fallback_response,
        ],
    ) as mock_run_gh:
        result = self_merge_check.fetch_greptile_status("gptme/gptme-contrib", 504)

    assert result["has_review"] is True
    # Verify --paginate was passed to the fallback REST call
    fallback_call_args = mock_run_gh.call_args_list[1].args[0]
    assert "--paginate" in fallback_call_args


def test_evaluate_pr_warns_on_auth_failure() -> None:
    """When get_gh_user() returns empty, a warning is emitted instead of silently skipping."""
    pr_data = {
        "author": {"login": "TimeToBuildBob"},
        "title": "Test PR",
        "url": "https://github.com/gptme/gptme-contrib/pull/999",
        "files": [{"path": "tests/test_example.py"}],
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "isDraft": False,
        "state": "OPEN",
        "baseRefName": "master",
    }

    with (
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value=""),
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

    assert any("author-identity check skipped" in w for w in result.warnings)
