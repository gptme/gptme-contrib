from __future__ import annotations

import importlib.util
import json
import subprocess
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


def test_parse_pr_target_strips_query_string() -> None:
    """URLs with query params (e.g. ?tab=files) must parse correctly, not raise ValueError."""
    repo, number = self_merge_check.parse_pr_target(
        "https://github.com/gptme/gptme-contrib/pull/504?tab=files", None, None
    )
    assert repo == "gptme/gptme-contrib"
    assert number == 504


def test_parse_pr_target_strips_fragment() -> None:
    """URLs with fragment anchors must parse correctly."""
    repo, number = self_merge_check.parse_pr_target(
        "https://github.com/gptme/gptme-contrib/pull/504#discussion_r123", None, None
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
            workspace_repos=["gptme/gptme-contrib"],
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
    # Query is now passed as "-f", "query=<body>" — extract body from index 3
    first_args = mock_run_gh.call_args_list[0].args[0]
    second_args = mock_run_gh.call_args_list[1].args[0]
    assert "reviewThreads(first:100)" in first_args[3]
    # Cursor is passed as a typed "-f after=..." variable, not injected into query
    assert "-f" in second_args
    assert any(a == "after=cursor-1" for a in second_args)


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


def test_resolve_gate_helper_uses_workspace_sibling(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    script_path = (
        workspace / "gptme-contrib" / "scripts" / "github" / "self-merge-check.py"
    )
    helper = workspace / "scripts" / "github-rate-limit-health.sh"
    helper.parent.mkdir(parents=True)
    helper.write_text("#!/bin/sh\nexit 0\n")
    helper.chmod(0o755)

    with patch.dict("os.environ", {}, clear=True):
        resolved = self_merge_check._resolve_gate_helper(script_path)

    assert resolved == str(helper)


def test_resolve_gate_helper_does_not_walk_arbitrary_ancestors(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    script_path = (
        workspace / "gptme-contrib" / "scripts" / "github" / "self-merge-check.py"
    )
    unsafe_helper = workspace / "gptme-contrib" / "github-rate-limit-health.sh"
    unsafe_helper.parent.mkdir(parents=True)
    unsafe_helper.write_text("#!/bin/sh\nexit 0\n")
    unsafe_helper.chmod(0o755)

    with patch.dict("os.environ", {}, clear=True):
        resolved = self_merge_check._resolve_gate_helper(script_path)

    assert resolved is None


def test_resolve_gate_helper_rejects_missing_explicit_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing-helper.sh"

    with patch.dict(
        "os.environ",
        {self_merge_check.GATE_HELPER_ENV: str(missing)},
        clear=True,
    ):
        with pytest.raises(RuntimeError, match="points to a missing helper"):
            self_merge_check._resolve_gate_helper()


def test_main_returns_error_for_missing_explicit_gate_helper(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing-helper.sh"

    with (
        patch.dict(
            "os.environ",
            {self_merge_check.GATE_HELPER_ENV: str(missing)},
            clear=True,
        ),
        patch.object(
            self_merge_check.sys,
            "argv",
            ["self-merge-check.py", "--repo", "gptme/gptme-contrib", "123"],
        ),
    ):
        rc = self_merge_check.main()

    captured = capsys.readouterr()
    assert rc == 2
    assert (
        f"{self_merge_check.GATE_HELPER_ENV} points to a missing helper" in captured.err
    )


def test_resolve_gate_helper_rejects_non_executable_explicit_path(
    tmp_path: Path,
) -> None:
    """A configured GATE_HELPER_ENV pointing to a non-executable file must fail."""
    non_exec = tmp_path / "not-executable.sh"
    non_exec.write_text("#!/bin/sh\nexit 0\n")
    non_exec.chmod(0o644)  # readable but not executable

    with patch.dict(
        "os.environ",
        {self_merge_check.GATE_HELPER_ENV: str(non_exec)},
        clear=True,
    ):
        with pytest.raises(RuntimeError, match="is not executable"):
            self_merge_check._resolve_gate_helper()


def test_resolve_gate_helper_rejects_missing_explicit_path_legacy_env(
    tmp_path: Path,
) -> None:
    """A legacy BOB_GH_RATE_LIMIT_HELPER pointing to a missing file must name the legacy var in its error."""
    missing = tmp_path / "missing-helper.sh"

    with patch.dict(
        "os.environ",
        {self_merge_check.GATE_HELPER_ENV_LEGACY: str(missing)},
        clear=True,
    ):
        with pytest.raises(
            RuntimeError,
            match=f"{self_merge_check.GATE_HELPER_ENV_LEGACY} points to a missing helper",
        ):
            self_merge_check._resolve_gate_helper()


def test_resolve_gate_helper_rejects_non_executable_legacy_env(
    tmp_path: Path,
) -> None:
    """A legacy BOB_GH_RATE_LIMIT_HELPER pointing to a non-executable file must name the legacy var in its error."""
    non_exec = tmp_path / "not-executable.sh"
    non_exec.write_text("#!/bin/sh\nexit 0\n")
    non_exec.chmod(0o644)

    with patch.dict(
        "os.environ",
        {self_merge_check.GATE_HELPER_ENV_LEGACY: str(non_exec)},
        clear=True,
    ):
        with pytest.raises(
            RuntimeError,
            match=f"{self_merge_check.GATE_HELPER_ENV_LEGACY} is not executable",
        ):
            self_merge_check._resolve_gate_helper()


def test_resolve_gate_helper_primary_env_takes_precedence_in_error_message(
    tmp_path: Path,
) -> None:
    """When both GATE_HELPER_ENV and BOB_GH_RATE_LIMIT_HELPER are set, only the primary var is named in error."""
    missing = tmp_path / "missing-helper.sh"

    with patch.dict(
        "os.environ",
        {
            self_merge_check.GATE_HELPER_ENV: str(missing),
            self_merge_check.GATE_HELPER_ENV_LEGACY: "/some/other/path",
        },
        clear=True,
    ):
        with pytest.raises(
            RuntimeError,
            match=f"{self_merge_check.GATE_HELPER_ENV} points to a missing helper",
        ):
            self_merge_check._resolve_gate_helper()


def test_maybe_defer_for_rate_limit_honors_force_self_merge_check(
    tmp_path: Path,
) -> None:
    """FORCE_SELF_MERGE_CHECK=1 should bypass the rate-limit gate entirely."""
    # Set up a valid helper so the ONLY reason we skip is the force env var
    helper = tmp_path / "gate-helper.sh"
    helper.write_text("#!/bin/sh\necho 'rate limited'; exit 76\n")
    helper.chmod(0o755)

    with patch.dict(
        "os.environ",
        {
            self_merge_check.FORCE_SELF_MERGE_CHECK_ENV: "1",
            self_merge_check.GATE_HELPER_ENV: str(helper),
        },
        clear=True,
    ):
        result = self_merge_check._maybe_defer_for_rate_limit(json_output=False)

    # The force bypass means the gate is never run — return None to proceed.
    assert result is None


def test_maybe_defer_for_rate_limit_honors_legacy_force_self_merge_check(
    tmp_path: Path,
) -> None:
    """BOB_FORCE_SELF_MERGE_CHECK=1 (legacy name) should also bypass the gate."""
    helper = tmp_path / "gate-helper.sh"
    helper.write_text("#!/bin/sh\necho 'rate limited'; exit 76\n")
    helper.chmod(0o755)

    with patch.dict(
        "os.environ",
        {
            self_merge_check.FORCE_SELF_MERGE_CHECK_ENV_LEGACY: "1",
            self_merge_check.GATE_HELPER_ENV: str(helper),
        },
        clear=True,
    ):
        result = self_merge_check._maybe_defer_for_rate_limit(json_output=False)

    assert result is None


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

    with (
        patch.object(
            self_merge_check,
            "run_gh",
            side_effect=[json.dumps(pr_metadata)],
        ) as mock_run_gh,
        patch.object(
            self_merge_check.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=files_output
            ),
        ) as mock_subprocess_run,
    ):
        pr = self_merge_check.fetch_pr("gptme/gptme-contrib", 504)

    assert len(pr["files"]) == 105
    assert pr["files"][104]["path"] == "tests/test_104.py"
    # run_gh is called only for `gh pr view`; files now use subprocess.run directly
    assert mock_run_gh.call_count == 1
    assert "files" not in mock_run_gh.call_args_list[0].args[0][-1]
    subprocess_args = mock_subprocess_run.call_args.args[0]
    assert subprocess_args[:5] == [
        "gh",
        "api",
        "repos/gptme/gptme-contrib/pulls/504/files",
        "--paginate",
        "--jq",
    ]


def test_fetch_pr_files_returns_empty_list_for_pr_with_no_changed_files() -> None:
    """A PR with 0 changed files should return [] not raise RuntimeError."""
    with patch.object(
        self_merge_check.subprocess,
        "run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=""),
    ):
        files = self_merge_check._fetch_pr_files("gptme/gptme-contrib", 999)
    assert files == []


def test_fetch_greptile_review_data_returns_partial_on_mid_pagination_failure() -> None:
    """Mid-pagination failure should return already-collected data, not None."""
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

    with patch.object(
        self_merge_check,
        "run_gh",
        side_effect=[
            self_merge_check.json.dumps(first_page),
            "",  # second page fails (network error / timeout)
        ],
    ):
        result = self_merge_check._fetch_greptile_review_data(
            "gptme/gptme-contrib", 504
        )

    # Should return partial data from page 1, not None
    assert result is not None
    reviews, threads = result
    assert len(reviews) == 1
    assert reviews[0]["author"]["login"] == "greptile-apps"
    assert len(threads) == 1


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("packages/docker-deployment.yaml", True),
        ("packages/service-deployer.py", True),
        ("packages/author_utils.py", False),
        # oauth2 variants should also be caught
        ("packages/oauth2_client.py", True),
        ("src/oauth2/provider.py", True),
        # authentication/authorization compound forms must also be caught
        ("packages/authentication_service.py", True),
        ("src/authorization/policy.py", True),
        # camelCase filenames must also be caught (not just snake_case)
        ("packages/authToken.py", True),
        ("scripts/deployScript.py", True),
        ("src/oauthClient.py", True),
    ],
)
def test_is_sensitive_path_handles_deploy_word_forms(path: str, expected: bool) -> None:
    assert self_merge_check.is_sensitive_path(path) is expected


@pytest.mark.parametrize(
    "path",
    [
        "scripts/session-bandit.py",
        "scripts/session-bandit-v2.py",
        "scripts/session_bandit.py",
        "scripts/state-delta.py",
        "scripts/state_delta.py",
    ],
)
def test_classify_loop_control_paths_blocked(path: str) -> None:
    category, reasons = self_merge_check.classify_category([path])
    assert category is None
    assert any("sensitive" in reason.lower() for reason in reasons)


def test_evaluate_pr_warns_when_workspace_repos_empty() -> None:
    """Explicit opt-out (workspace_repos=[]) emits a warning but does not disqualify."""
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
            workspace_repos=[],  # explicit opt-out via WORKSPACE_REPO=''
        )

    # Explicit opt-out → warning, but PR is still eligible
    assert any("cross-repo restriction" in w for w in result.warnings)
    assert (
        result.eligible
    ), f"Explicit opt-out should not disqualify; reasons: {result.reasons}"


def test_evaluate_pr_disqualified_when_workspace_repos_unknown() -> None:
    """Detection failure (workspace_repos=None) must disqualify the PR."""
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
            workspace_repos=None,  # detection failed — unknown workspace
        )

    # Detection failure → disqualified (not just a warning)
    assert not result.eligible
    assert any("auto-detected" in r for r in result.reasons)


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


def test_evaluate_pr_ineligible_on_auth_failure() -> None:
    """When get_gh_user() returns empty, the PR is disqualified (author-identity check fails)."""
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
            workspace_repos=["gptme/gptme-contrib"],
        )

    assert any("author-identity check failed" in r for r in result.reasons)
    assert not result.eligible


def test_resolve_workspace_repos_honors_empty_env_opt_out() -> None:
    """WORKSPACE_REPO='' should disable the cross-repo restriction (opt-out)."""
    import argparse
    import os

    args = argparse.Namespace(workspace_repo=None)

    with patch.dict(os.environ, {"WORKSPACE_REPO": ""}, clear=False):
        workspace_repos = self_merge_check._resolve_workspace_repos(args)

    assert workspace_repos == [], (
        f"WORKSPACE_REPO='' should yield [] so cross-repo restriction is "
        f"disabled; got: {workspace_repos!r}"
    )


# --- WORKSPACE_REPO allowlist parsing + cross-repo check --------------------


def test_parse_workspace_repos_none() -> None:
    assert self_merge_check._parse_workspace_repos(None) is None


def test_parse_workspace_repos_empty_is_opt_out() -> None:
    # Explicit empty string opts out of the cross-repo restriction entirely.
    assert self_merge_check._parse_workspace_repos("") == []
    assert self_merge_check._parse_workspace_repos("   ") == []


def test_parse_workspace_repos_comma_separated() -> None:
    assert self_merge_check._parse_workspace_repos("a/b,c/d") == ["a/b", "c/d"]


def test_parse_workspace_repos_whitespace_separated() -> None:
    assert self_merge_check._parse_workspace_repos("a/b c/d") == ["a/b", "c/d"]


def test_parse_workspace_repos_mixed_and_trimmed() -> None:
    # Trailing spaces, spaces around commas, multiple whitespace — all normalized.
    assert self_merge_check._parse_workspace_repos("  a/b, c/d , e/f  ") == [
        "a/b",
        "c/d",
        "e/f",
    ]


def test_parse_workspace_repos_skips_empty_entries() -> None:
    assert self_merge_check._parse_workspace_repos("a/b,,c/d") == ["a/b", "c/d"]


def test_check_workspace_repo_detection_failed_disqualifies() -> None:
    reasons, warnings = self_merge_check._check_workspace_repo("gptme/gptme", None)
    assert reasons and "could not be auto-detected" in reasons[0]
    assert warnings == []


def test_check_workspace_repo_opt_out_warns_but_allows() -> None:
    reasons, warnings = self_merge_check._check_workspace_repo("gptme/gptme", [])
    assert reasons == []
    assert warnings and "cross-repo restriction is not enforced" in warnings[0]


def test_check_workspace_repo_allowed_single() -> None:
    reasons, warnings = self_merge_check._check_workspace_repo(
        "ErikBjare/bob", ["ErikBjare/bob"]
    )
    assert reasons == []
    assert warnings == []


def test_check_workspace_repo_allowed_in_multi() -> None:
    reasons, warnings = self_merge_check._check_workspace_repo(
        "gptme/gptme",
        ["ErikBjare/bob", "gptme/gptme", "gptme/gptme-contrib"],
    )
    assert reasons == []
    assert warnings == []


def test_check_workspace_repo_not_in_allowlist_disqualifies() -> None:
    reasons, warnings = self_merge_check._check_workspace_repo(
        "some-other/repo",
        ["ErikBjare/bob", "gptme/gptme"],
    )
    assert warnings == []
    assert reasons
    # Error must surface both the PR repo and the allowed list so the user
    # can see at a glance why the PR was rejected and what to add.
    assert "some-other/repo" in reasons[0]
    assert "ErikBjare/bob" in reasons[0]
    assert "gptme/gptme" in reasons[0]


# --- repo-path allowlist tests ---


def test_parse_repo_path_allowlist_space_separated() -> None:
    result = self_merge_check._parse_repo_path_allowlist(
        "TimeToBuildBob/whatdidyougetdone:whatdidyougetdone.py OtherOrg/repo:src/*.py"
    )
    assert "TimeToBuildBob/whatdidyougetdone" in result
    assert result["TimeToBuildBob/whatdidyougetdone"] == ["whatdidyougetdone.py"]
    assert "OtherOrg/repo" in result
    assert result["OtherOrg/repo"] == ["src/*.py"]


def test_parse_repo_path_allowlist_comma_separated() -> None:
    result = self_merge_check._parse_repo_path_allowlist(
        "TimeToBuildBob/whatdidyougetdone:whatdidyougetdone.py,OtherOrg/repo:src/*.py"
    )
    assert result["TimeToBuildBob/whatdidyougetdone"] == ["whatdidyougetdone.py"]
    assert result["OtherOrg/repo"] == ["src/*.py"]


def test_parse_repo_path_allowlist_empty() -> None:
    assert self_merge_check._parse_repo_path_allowlist("") == {}
    assert self_merge_check._parse_repo_path_allowlist(None) == {}


def test_parse_repo_path_allowlist_invalid_entries_ignored() -> None:
    # Entries without a colon or with empty sides are skipped
    result = self_merge_check._parse_repo_path_allowlist(
        "just-plain-text :orphan_pattern orphan_repo:"
    )
    assert result == {}


def test_is_repo_allowlisted_path_basic_match() -> None:
    allowlist = {"TimeToBuildBob/whatdidyougetdone": ["whatdidyougetdone.py"]}
    assert self_merge_check.is_repo_allowlisted_path(
        "whatdidyougetdone.py", "TimeToBuildBob/whatdidyougetdone", allowlist
    )
    assert not self_merge_check.is_repo_allowlisted_path(
        "other.py", "TimeToBuildBob/whatdidyougetdone", allowlist
    )


def test_is_repo_allowlisted_path_empty_repo_or_allowlist() -> None:
    assert not self_merge_check.is_repo_allowlisted_path("f.py", None)
    assert not self_merge_check.is_repo_allowlisted_path("f.py", "repo", {})


def test_is_repo_allowlisted_path_star_does_not_cross_dirs() -> None:
    """Single-star globs stay within one directory level."""
    allowlist = {"repo": ["src/*.py"]}
    # Should match a file directly in src/
    assert self_merge_check.is_repo_allowlisted_path("src/main.py", "repo", allowlist)
    # Should NOT match a file in a subdirectory
    assert not self_merge_check.is_repo_allowlisted_path(
        "src/subdir/secret.py", "repo", allowlist
    )


def test_is_repo_allowlisted_path_double_star_crosses_dirs() -> None:
    """Globstar should match zero or more directory segments."""
    allowlist = {"repo": ["src/**/*.py"]}
    assert self_merge_check.is_repo_allowlisted_path("src/main.py", "repo", allowlist)
    assert self_merge_check.is_repo_allowlisted_path(
        "src/subdir/secret.py", "repo", allowlist
    )
    assert self_merge_check.is_repo_allowlisted_path(
        "src/subdir/deep/nested/main.py", "repo", allowlist
    )


def test_classify_repo_allowlisted_path_allowed() -> None:
    with patch.dict(
        "os.environ",
        {
            "SELF_MERGE_ALLOWED_PATHS": "TimeToBuildBob/whatdidyougetdone:whatdidyougetdone.py"
        },
    ):
        category, reasons = self_merge_check.classify_category(
            ["whatdidyougetdone.py"], repo="TimeToBuildBob/whatdidyougetdone"
        )
        assert category == "repo-allowlisted(TimeToBuildBob/whatdidyougetdone)"
        assert reasons == []


def test_classify_repo_allowlisted_path_is_repo_scoped() -> None:
    with patch.dict(
        "os.environ",
        {
            "SELF_MERGE_ALLOWED_PATHS": "TimeToBuildBob/whatdidyougetdone:whatdidyougetdone.py"
        },
    ):
        category, reasons = self_merge_check.classify_category(
            ["whatdidyougetdone.py"], repo="TimeToBuildBob/other-repo"
        )
        assert category is None
        assert any("allowed self-merge category" in reason for reason in reasons)
