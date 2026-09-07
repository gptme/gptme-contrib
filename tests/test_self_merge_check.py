from __future__ import annotations

import dataclasses
import importlib.util
import json
import subprocess
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
spec = importlib.util.spec_from_file_location("self_merge_check", MODULE_PATH)
if spec is None or spec.loader is None:
    pytest.skip(f"Could not load module from {MODULE_PATH}", allow_module_level=True)
self_merge_check = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = self_merge_check
spec.loader.exec_module(self_merge_check)

# Real (unpatched) helper, captured before the autouse fixture swaps it for a Mock.
_REAL_MERGE_PERMISSION = self_merge_check.merge_permission.__wrapped__


@pytest.fixture(autouse=True)
def _stub_external_merge_checks():
    """Keep evaluate_pr tests independent of GitHub access.

    The permission and AI-review helpers both make live ``gh`` calls and fail
    closed when their state is unreadable. Tests for those gates patch the
    helpers explicitly.
    """
    self_merge_check.merge_permission.cache_clear()
    with (
        patch.object(self_merge_check, "merge_permission", return_value=True),
        patch.object(self_merge_check, "ai_review_abstained", return_value=False),
        # contrib_classify_category's agent-literal check makes its own live
        # ``gh api .../files`` call once a gptme-contrib PR is otherwise
        # eligible (see evaluate_pr). Default to "no added lines to scan" so
        # ordinary evaluate_pr tests don't depend on GitHub access or on
        # whatever PR #999 happens to contain; tests targeting the
        # agent-literal check itself override this explicitly.
        patch.object(
            self_merge_check, "_fetch_contrib_pr_file_shapes", return_value=[]
        ),
        # Same for gptme/gptme's diff-shape fetch: only gptme-core tests
        # that need real patch text override this.
        patch.object(self_merge_check, "_fetch_pr_file_shapes", return_value=[]),
    ):
        yield
    self_merge_check.merge_permission.cache_clear()


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


def test_checks_green_allows_success_statuscontext() -> None:
    # StatusContext (legacy commit status, e.g. codecov/patch) carries its
    # result in `state` with no status/conclusion. A green one must pass.
    assert self_merge_check.checks_green(
        [
            {
                "__typename": "StatusContext",
                "context": "codecov/patch",
                "state": "SUCCESS",
            }
        ]
    )


def test_checks_green_rejects_non_success_statuscontext() -> None:
    for bad in ("PENDING", "FAILURE", "ERROR", "EXPECTED"):
        assert not self_merge_check.checks_green(
            [{"__typename": "StatusContext", "context": "ci", "state": bad}]
        )


def test_checks_green_mixed_checkrun_and_statuscontext() -> None:
    # A green CheckRun alongside a green StatusContext should pass.
    assert self_merge_check.checks_green(
        [
            {"status": "COMPLETED", "conclusion": "SUCCESS"},
            {"state": "SUCCESS"},
        ]
    )


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


def test_parse_pr_target_accepts_repo_hash_number() -> None:
    """The documented 'owner/repo#number' specifier must parse."""
    repo, number = self_merge_check.parse_pr_target(
        "ActivityWatch/activitywatch.github.io#54", None, None
    )
    assert repo == "ActivityWatch/activitywatch.github.io"
    assert number == 54


def test_parse_pr_target_accepts_repo_and_positional_number() -> None:
    """Two-positional 'owner/repo <number>' form must parse, not raise."""
    repo, number = self_merge_check.parse_pr_target(
        "ActivityWatch/activitywatch.github.io", None, 54
    )
    assert repo == "ActivityWatch/activitywatch.github.io"
    assert number == 54


def test_parse_pr_target_repo_without_number_raises_with_hint() -> None:
    """A bare 'owner/repo' (no number anywhere) must raise a guiding error."""
    import pytest

    with pytest.raises(ValueError, match="owner/repo#number"):
        self_merge_check.parse_pr_target("gptme/gptme", None, None)


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
        "labels": [],
    }

    with (
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(
            self_merge_check, "_fetch_greptile_review_data", return_value=None
        ),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": True, "unresolved": 0, "total": 1},
        ),
        patch.object(self_merge_check, "greptile_summary_score", return_value=None),
        patch.object(
            self_merge_check,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "total": 0, "authors": []},
        ),
        patch.object(
            self_merge_check,
            "fetch_ai_review_status",
            return_value={"accepted": True, "detail": "AI review accepted"},
        ),
    ):
        result = self_merge_check.evaluate_pr(
            "gptme/gptme-contrib",
            999,
            workspace_repos=["gptme/gptme-contrib"],
        )

    assert not result.eligible
    assert "Review decision: CHANGES_REQUESTED" in result.reasons
    # Greptile is now advisory so it appears in warnings — only check no blocking reasons beyond CHANGES_REQUESTED
    assert not any(r for r in result.reasons if "CHANGES_REQUESTED" not in r)


def _make_clean_pr_data(**overrides: object) -> dict[str, object]:
    """Minimal PR data that passes all other checks. Override fields to test specific gates."""
    data: dict[str, object] = {
        "author": {"login": "TimeToBuildBob"},
        "title": "Test PR",
        "url": "https://github.com/gptme/gptme-contrib/pull/999",
        "files": [{"path": "tests/test_example.py"}],
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "isDraft": False,
        "state": "OPEN",
        "reviewDecision": None,
        "headRefOid": "abc123",
        "mergeStateStatus": "CLEAN",
        "labels": [],
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize("merge_state", ["DIRTY"])
def test_evaluate_pr_blocks_merge_conflicts(merge_state: str) -> None:
    pr_data = _make_clean_pr_data(mergeStateStatus=merge_state)

    with (
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(
            self_merge_check, "_fetch_greptile_review_data", return_value=None
        ),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": True, "unresolved": 0, "total": 1},
        ),
        patch.object(self_merge_check, "greptile_summary_score", return_value=None),
        patch.object(
            self_merge_check,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "total": 0, "authors": []},
        ),
    ):
        result = self_merge_check.evaluate_pr(
            "gptme/gptme-contrib",
            999,
            workspace_repos=["gptme/gptme-contrib"],
        )

    assert not result.eligible
    assert any("merge conflicts" in r for r in result.reasons)
    assert merge_state in " ".join(result.reasons)


def test_evaluate_pr_clean_merge_state_passes_gate() -> None:
    pr_data = _make_clean_pr_data(mergeStateStatus="CLEAN")

    with (
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(
            self_merge_check, "_fetch_greptile_review_data", return_value=None
        ),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": True, "unresolved": 0, "total": 1},
        ),
        patch.object(self_merge_check, "greptile_summary_score", return_value=None),
        patch.object(
            self_merge_check,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "total": 0, "authors": []},
        ),
        patch.object(
            self_merge_check,
            "fetch_ai_review_status",
            return_value={"accepted": True, "detail": "AI review 5/5 at current head"},
        ),
    ):
        result = self_merge_check.evaluate_pr(
            "gptme/gptme-contrib",
            999,
            workspace_repos=["gptme/gptme-contrib"],
        )

    assert result.eligible
    assert not any("merge conflicts" in r for r in result.reasons)


def _evaluate_with_hold_labels(labels: list[object]) -> Any:
    pr_data = _make_clean_pr_data(labels=labels)
    with (
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(
            self_merge_check, "_fetch_greptile_review_data", return_value=None
        ),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": True, "unresolved": 0, "total": 1},
        ),
        patch.object(self_merge_check, "greptile_summary_score", return_value=None),
        patch.object(
            self_merge_check,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "total": 0, "authors": []},
        ),
        patch.object(
            self_merge_check,
            "fetch_ai_review_status",
            return_value={"accepted": True, "detail": "AI review 5/5 at current head"},
        ),
    ):
        return self_merge_check.evaluate_pr(
            "gptme/gptme-contrib",
            999,
            workspace_repos=["gptme/gptme-contrib"],
        )


@pytest.mark.parametrize("label_name", ["do-not-merge", "hold", "on-hold"])
def test_evaluate_pr_blocks_operator_hold_label(label_name: str) -> None:
    result = _evaluate_with_hold_labels([{"name": label_name}])
    assert not result.eligible
    assert any("Operator hold" in r for r in result.reasons)
    assert label_name in " ".join(result.reasons)


@pytest.mark.parametrize("label_name", ["Do-Not-Merge", "HOLD", "On-Hold"])
def test_evaluate_pr_blocks_operator_hold_label_case_insensitive(
    label_name: str,
) -> None:
    """Hold label matching must be case-insensitive.

    Labels applied via the GitHub UI use the label's stored name, but a future
    rename (e.g. 'hold' → 'Hold') must not silently bypass the gate.
    """
    result = _evaluate_with_hold_labels([{"name": label_name}])
    assert not result.eligible
    assert any("Operator hold" in r for r in result.reasons)


@pytest.mark.parametrize("label_name", ["do-not-merge ", " hold", " on-hold "])
def test_evaluate_pr_blocks_operator_hold_label_with_whitespace(
    label_name: str,
) -> None:
    """Hold label matching must strip surrounding whitespace.

    GitHub labels can be applied with accidental leading/trailing spaces when
    created or renamed via the UI or API. A 'do-not-merge ' (trailing space)
    label should still block the merge — the .strip() call in the detection
    logic is the guard; this test pins that contract.
    """
    result = _evaluate_with_hold_labels([{"name": label_name}])
    assert not result.eligible
    assert any("Operator hold" in r for r in result.reasons)


def test_evaluate_pr_no_hold_label_is_eligible() -> None:
    result = _evaluate_with_hold_labels([{"name": "enhancement"}, {"name": "ci"}])
    assert result.eligible
    assert not any("Operator hold" in r for r in result.reasons)


def test_evaluate_pr_malformed_label_elements_do_not_crash() -> None:
    """Non-dict elements and null name values in the labels list must fail closed (block merge).

    A cache shim or API change could return labels as a mixed list.
    Non-dict entries and non-string name values are unverifiable, so we block the merge
    rather than silently skip them (fail-closed security posture to prevent hold bypass).
    """
    # Mix of non-dict elements and a dict with name=None — all are unverifiable
    result = _evaluate_with_hold_labels(["hold", None, {"name": None}, {"name": "ci"}])
    assert not result.eligible
    assert any("unparseable" in r.lower() for r in result.reasons)


def test_evaluate_pr_null_labels_fails_closed() -> None:
    """Labels key present but null must fail closed — same as a missing key.

    A cache shim or malformed payload returning labels=null must not accidentally
    pass the hold check. Null is unverifiable, so we block like we do for a
    missing key rather than treating it as 'no hold labels present'.
    """
    pr_data = _make_clean_pr_data(labels=None)
    with (
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(
            self_merge_check, "_fetch_greptile_review_data", return_value=None
        ),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": True, "unresolved": 0, "total": 1},
        ),
        patch.object(self_merge_check, "greptile_summary_score", return_value=None),
        patch.object(
            self_merge_check,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "total": 0, "authors": []},
        ),
    ):
        result = self_merge_check.evaluate_pr(
            "gptme/gptme-contrib",
            999,
            workspace_repos=["gptme/gptme-contrib"],
        )
    assert not result.eligible
    assert any("labels missing" in r for r in result.reasons)


def test_evaluate_pr_missing_labels_key_blocks_merge() -> None:
    """Labels key entirely absent from payload must fail closed (not allow merge)."""
    pr_data = _make_clean_pr_data()
    del pr_data["labels"]  # simulate payload missing the field entirely
    with (
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(
            self_merge_check, "_fetch_greptile_review_data", return_value=None
        ),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": True, "unresolved": 0, "total": 1},
        ),
        patch.object(self_merge_check, "greptile_summary_score", return_value=None),
        patch.object(
            self_merge_check,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "total": 0, "authors": []},
        ),
    ):
        result = self_merge_check.evaluate_pr(
            "gptme/gptme-contrib",
            999,
            workspace_repos=["gptme/gptme-contrib"],
        )
    assert not result.eligible
    assert any("labels missing" in r.lower() for r in result.reasons)


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
        "labels": [],
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
        "skills/example/.env",
        "skills/example/.env.local",
        "skills/example/prod.env",
        "skills/example/config.env",
        "skills/example/prod.env.local",
        "skills/example/config.env.production",
        "skills/example/certificate.PEM",
        "skills/example/private.key",
        "skills/example/identity.p12",
        "skills/example/identity.pfx",
        "skills/example/.ssh/id_rsa",
        "skills/example/.SSH/authorized_keys",
        "skills/example/id_ed25519",
        "skills/example/password.txt",
        "skills/example/passwords.json",
        "skills/example/api_key.py",
        "skills/example/apikey.json",
        "skills/example/access_token.txt",
        "skills/example/accessToken.py",
        # Dotfiles with sensitive keywords — stem must not become empty (P0 fix)
        "skills/example/.secret",
        "skills/example/.token",
        "skills/example/.password",
        "skills/example/.credentials",
        # Private-key filenames with backup/rotation suffixes (P1 fix)
        "skills/example/id_rsa.old",
        "skills/example/id_rsa.bak",
        "skills/example/id_ed25519.old",
        "skills/example/id_ecdsa.bak",
    ],
)
def test_secret_bearing_file_formats_are_sensitive(path: str) -> None:
    assert self_merge_check.is_sensitive_path(path)
    assert not self_merge_check.is_allowed_file(path)
    category, reasons = self_merge_check.classify_category([path])
    assert category is None
    assert any("sensitive" in reason.lower() for reason in reasons)


@pytest.mark.parametrize(
    "path",
    [
        "packages/example/env.py",
        "skills/example/env.json",
    ],
)
def test_env_named_modules_are_not_sensitive(path: str) -> None:
    assert not self_merge_check.is_sensitive_path(path)
    assert self_merge_check.is_allowed_file(path)
    category, reasons = self_merge_check.classify_category([path])
    assert category is not None, reasons


@pytest.mark.parametrize(
    "path",
    [
        "scripts/session-bandit.py",
        "scripts/session-bandit-v2.py",
        "scripts/session_bandit.py",
        "scripts/state-delta.py",
        "scripts/state_delta.py",
        # autonomous/monitoring orchestration entrypoints (the self-merge control path)
        "scripts/autonomous-run.sh",
        "scripts/autonomous-run-cc.sh",
        "scripts/autonomous_run.py",
        "scripts/autonomous-loop.sh",
        "scripts/runs/autonomous/autonomous-loop.sh",
        "scripts/runs/github/project-monitoring.sh",
        "scripts/runs/github/project_monitoring.py",
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
        "labels": [],
    }

    with (
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(
            self_merge_check, "_fetch_greptile_review_data", return_value=None
        ),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": True, "unresolved": 0, "total": 1},
        ),
        patch.object(self_merge_check, "greptile_summary_score", return_value=5),
        patch.object(
            self_merge_check,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "total": 0, "authors": []},
        ),
        patch.object(
            self_merge_check,
            "fetch_ai_review_status",
            return_value={"accepted": True, "detail": "AI review 5/5 at current head"},
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


def _eligible_pr_data() -> dict:
    return {
        "author": {"login": "TimeToBuildBob"},
        "title": "Test PR",
        "url": "https://github.com/gptme/gptme-contrib/pull/999",
        "files": [{"path": "tests/test_example.py"}],
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "isDraft": False,
        "state": "OPEN",
        "reviewDecision": None,
        "labels": [],
    }


def _evaluate_otherwise_eligible(merge_perm):
    """Evaluate an otherwise-eligible PR with a given merge_permission result."""
    with (
        patch.object(self_merge_check, "fetch_pr", return_value=_eligible_pr_data()),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(
            self_merge_check, "_fetch_greptile_review_data", return_value=None
        ),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": True, "unresolved": 0, "total": 1},
        ),
        patch.object(self_merge_check, "greptile_summary_score", return_value=5),
        patch.object(
            self_merge_check,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "total": 0, "authors": []},
        ),
        patch.object(
            self_merge_check,
            "fetch_ai_review_status",
            return_value={"accepted": True, "detail": "AI review 5/5 at current head"},
        ),
        patch.object(self_merge_check, "merge_permission", return_value=merge_perm),
    ):
        return self_merge_check.evaluate_pr(
            "gptme/gptme-contrib",
            999,
            workspace_repos=["gptme/gptme-contrib"],
        )


def test_evaluate_pr_blocks_when_no_merge_permission() -> None:
    """A definitive lack of merge permission disqualifies (would fail the merge)."""
    result = _evaluate_otherwise_eligible(False)
    assert not result.eligible
    assert any("lacks merge permission" in r for r in result.reasons)


def test_evaluate_pr_warns_when_merge_permission_unknown() -> None:
    """An indeterminate merge permission is a warning, not a disqualifier."""
    result = _evaluate_otherwise_eligible(None)
    assert result.eligible, f"reasons: {result.reasons}"
    assert any("determine merge permission" in w for w in result.warnings)


def test_evaluate_pr_eligible_with_merge_permission() -> None:
    """Explicit merge permission keeps an otherwise-eligible PR eligible."""
    result = _evaluate_otherwise_eligible(True)
    assert result.eligible, f"reasons: {result.reasons}"


def test_merge_permission_raw_helper() -> None:
    """The underlying helper maps gh permission payloads to a bool/None."""
    raw = _REAL_MERGE_PERMISSION
    with patch.object(
        self_merge_check, "run_gh", return_value='{"push": true, "admin": false}'
    ):
        assert raw("gptme/gptme-contrib") is True
    with patch.object(
        self_merge_check,
        "run_gh",
        return_value='{"push": false, "maintain": false, "admin": false}',
    ):
        assert raw("gptme/gptme-contrib") is False
    with patch.object(self_merge_check, "run_gh", return_value=""):
        assert raw("gptme/gptme-contrib") is None
    with patch.object(self_merge_check, "run_gh", return_value="not json"):
        assert raw("gptme/gptme-contrib") is None
    # Dict present but all three expected keys absent — unknown, not disqualified
    with patch.object(
        self_merge_check,
        "run_gh",
        return_value='{"pull": true, "triage": true}',
    ):
        assert raw("gptme/gptme-contrib") is None


def test_greptile_summary_score_ignores_signal_disable_env() -> None:
    completed = subprocess.CompletedProcess(
        args=["python3"],
        returncode=1,
        stdout='{"score": 4}',
        stderr="",
    )

    with (
        patch.dict(
            self_merge_check.os.environ,
            {"GREPTILE_MERGE_SIGNAL_DISABLED": "1"},
            clear=False,
        ),
        patch.object(
            self_merge_check.subprocess, "run", return_value=completed
        ) as mock_run,
    ):
        score = self_merge_check.greptile_summary_score("gptme/gptme-contrib", 1080)

    assert score == 4
    assert "GREPTILE_MERGE_SIGNAL_DISABLED" not in mock_run.call_args.kwargs["env"]


def test_greptile_stale_score_gated_by_ai_review() -> None:
    """Stale Greptile (score 4/5, reviewed at old head) must not block an otherwise
    eligible PR. AI review accepted → eligible; Greptile floor bypassed because its
    review does not cover the current head (branch 2 of the 3-branch staleness policy)."""
    pr_data = {
        "author": {"login": "TimeToBuildBob"},
        "title": "Test PR",
        "url": "https://github.com/gptme/gptme-contrib/pull/999",
        "number": 999,
        "headRefOid": "abc1234",
        "files": [{"path": "tests/test_example.py"}],
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "isDraft": False,
        "state": "OPEN",
        "reviewDecision": None,
        "labels": [],
        "isCrossRepository": False,
        "baseRefName": "master",
        "mergeStateStatus": "CLEAN",
    }

    with (
        patch.object(
            self_merge_check, "_fetch_greptile_review_data", return_value=([], [])
        ),
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": True, "unresolved": 0, "total": 1},
        ),
        patch.object(self_merge_check, "greptile_summary_score", return_value=4),
        # Stale: reviewed at a different (old) head → not fresh
        patch.object(
            self_merge_check,
            "greptile_summary_reviewed_commit",
            return_value="deadbeef1234",
        ),
        patch.object(
            self_merge_check,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "authors": []},
        ),
        patch.object(
            self_merge_check,
            "fetch_ai_review_status",
            return_value={
                "accepted": True,
                "detail": "AI review 5/5 at current head abc1234, findings disposed",
            },
        ),
    ):
        result = self_merge_check.evaluate_pr(
            "gptme/gptme-contrib",
            999,
            workspace_repos=["gptme/gptme-contrib"],
        )

    # Stale Greptile → AI review is the gate → AI review accepted → eligible
    assert result.eligible, f"Expected eligible but got reasons: {result.reasons}"
    assert not any("floor" in r for r in result.reasons), result.reasons
    assert not any("Greptile score" in r for r in result.reasons), result.reasons
    # Greptile stale info should appear in warnings (not as a reason)
    assert any(
        "stale" in w and "AI review is the gate" in w for w in result.warnings
    ), result.warnings


def test_greptile_stale_score_blocked_by_ai_review_not_greptile() -> None:
    """When Greptile is 4/5 (stale) and AI review has not run, the block reason must be
    'AI review required', not 'Greptile score below floor' (branch 3: neither fresh)."""
    pr_data = {
        "author": {"login": "TimeToBuildBob"},
        "title": "Test PR",
        "url": "https://github.com/gptme/gptme-contrib/pull/999",
        "number": 999,
        "headRefOid": "abc1234",
        "files": [{"path": "tests/test_example.py"}],
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "isDraft": False,
        "state": "OPEN",
        "reviewDecision": None,
        "labels": [],
        "isCrossRepository": False,
        "baseRefName": "master",
        "mergeStateStatus": "CLEAN",
    }

    with (
        patch.object(
            self_merge_check, "_fetch_greptile_review_data", return_value=([], [])
        ),
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": True, "unresolved": 0, "total": 1},
        ),
        patch.object(self_merge_check, "greptile_summary_score", return_value=4),
        # Stale: reviewed at a different head → not fresh
        patch.object(
            self_merge_check,
            "greptile_summary_reviewed_commit",
            return_value="deadbeef1234",
        ),
        patch.object(
            self_merge_check,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "authors": []},
        ),
        patch.object(
            self_merge_check,
            "fetch_ai_review_status",
            return_value={
                "accepted": False,
                "detail": "AI review marker lookup failed",
            },
        ),
    ):
        result = self_merge_check.evaluate_pr(
            "gptme/gptme-contrib",
            999,
            workspace_repos=["gptme/gptme-contrib"],
        )

    assert not result.eligible
    assert any("AI review required" in r for r in result.reasons), result.reasons
    assert not any("floor" in r for r in result.reasons), result.reasons


def test_greptile_fresh_score_below_floor_blocks() -> None:
    """When Greptile reviews the current head (fresh) with score 4/5, the 5/5 floor
    applies and the PR must be blocked (branch 1 of the 3-branch staleness policy)."""
    pr_data = {
        "author": {"login": "TimeToBuildBob"},
        "title": "Test PR",
        "url": "https://github.com/gptme/gptme-contrib/pull/999",
        "number": 999,
        "headRefOid": "abc1234",
        "files": [{"path": "tests/test_example.py"}],
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "isDraft": False,
        "state": "OPEN",
        "reviewDecision": None,
        "labels": [],
        "isCrossRepository": False,
        "baseRefName": "master",
        "mergeStateStatus": "CLEAN",
    }

    with (
        patch.object(
            self_merge_check, "_fetch_greptile_review_data", return_value=([], [])
        ),
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": True, "unresolved": 0, "total": 1},
        ),
        patch.object(self_merge_check, "greptile_summary_score", return_value=4),
        # Fresh: reviewed at the current head
        patch.object(
            self_merge_check,
            "greptile_summary_reviewed_commit",
            return_value="abc1234",
        ),
        patch.object(
            self_merge_check,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "authors": []},
        ),
        patch.object(
            self_merge_check,
            "fetch_ai_review_status",
            return_value={
                "accepted": True,
                "detail": "AI review 5/5 at current head abc1234, findings disposed",
            },
        ),
    ):
        result = self_merge_check.evaluate_pr(
            "gptme/gptme-contrib",
            999,
            workspace_repos=["gptme/gptme-contrib"],
        )

    # Fresh Greptile 4/5 < floor 5/5 → blocked even if AI review accepted
    assert (
        not result.eligible
    ), f"Expected blocked but got eligible. Warnings: {result.warnings}"
    assert any(
        "Greptile score" in r and "below floor" in r for r in result.reasons
    ), result.reasons


def test_greptile_fresh_score_at_floor_eligible() -> None:
    """Fresh Greptile 5/5 satisfies the floor; PR is eligible (branch 1, floor met)."""
    pr_data = {
        "author": {"login": "TimeToBuildBob"},
        "title": "Test PR",
        "url": "https://github.com/gptme/gptme-contrib/pull/999",
        "number": 999,
        "headRefOid": "abc1234",
        "files": [{"path": "tests/test_example.py"}],
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "isDraft": False,
        "state": "OPEN",
        "reviewDecision": None,
        "labels": [],
        "isCrossRepository": False,
        "baseRefName": "master",
        "mergeStateStatus": "CLEAN",
    }

    with (
        patch.object(
            self_merge_check, "_fetch_greptile_review_data", return_value=([], [])
        ),
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": True, "unresolved": 0, "total": 1},
        ),
        patch.object(self_merge_check, "greptile_summary_score", return_value=5),
        # Fresh: reviewed at the current head
        patch.object(
            self_merge_check,
            "greptile_summary_reviewed_commit",
            return_value="abc1234",
        ),
        patch.object(
            self_merge_check,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "authors": []},
        ),
        patch.object(
            self_merge_check,
            "fetch_ai_review_status",
            return_value={
                "accepted": True,
                "detail": "AI review 5/5 at current head abc1234, findings disposed",
            },
        ),
    ):
        result = self_merge_check.evaluate_pr(
            "gptme/gptme-contrib",
            999,
            workspace_repos=["gptme/gptme-contrib"],
        )

    assert result.eligible, f"Expected eligible but got reasons: {result.reasons}"
    assert any("floor satisfied" in w for w in result.warnings), result.warnings


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

    # The GraphQL fetch still goes through `run_gh`; the summary-comment
    # fallback uses `run_gh_checked`, which distinguishes a failed call from an
    # empty result so a timeout cannot read as "no Greptile review".
    with (
        patch.object(
            self_merge_check,
            "run_gh",
            side_effect=[self_merge_check.json.dumps(graphql_page)],
        ),
        patch.object(
            self_merge_check,
            "run_gh_checked",
            side_effect=[fallback_response],
        ) as mock_fallback,
    ):
        result = self_merge_check.fetch_greptile_status("gptme/gptme-contrib", 504)

    assert result["has_review"] is True
    assert not result.get("unknown")
    # Verify --paginate was passed to the fallback REST call
    fallback_call_args = mock_fallback.call_args_list[0].args[0]
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


def test_classify_sensitive_path_overridden_by_allowlist() -> None:
    """Explicitly allowlisted paths pass even when the keyword scanner flags them.

    Real case: LLM tokenizer utility files like gptme/util/tokens.py are flagged by
    the 'token' keyword heuristic, but they are not auth/security sensitive.
    An operator can add them to SELF_MERGE_ALLOWED_PATHS to unblock auto-merge.
    Both the source file and its test file must be allowlisted.
    """
    with patch.dict(
        "os.environ",
        {
            "SELF_MERGE_ALLOWED_PATHS": (
                "gptme/gptme:gptme/util/**,gptme/gptme:tests/test_util_tokens.py"
            )
        },
    ):
        # Without the fix, both paths would be blocked by "Touches sensitive/security/infra paths"
        # because "tokens" stem matches the "token" keyword with the plural-s rule.
        category, reasons = self_merge_check.classify_category(
            ["gptme/util/tokens.py", "tests/test_util_tokens.py"],
            repo="gptme/gptme",
        )
        assert category is not None, f"Expected allowed, got reasons: {reasons}"
        assert not any("sensitive" in r.lower() for r in reasons)


def test_is_allowed_file_allowlist_overrides_sensitive_path() -> None:
    """is_allowed_file returns True for an allowlisted path even when it is sensitive."""
    allowlist = {"gptme/gptme": ["gptme/util/**"]}
    # Without allowlist: blocked by the sensitive-path heuristic ("token" → "tokens")
    assert not self_merge_check.is_allowed_file("gptme/util/tokens.py")
    # With explicit allowlist: allowed (operator override takes precedence over heuristic)
    assert self_merge_check.is_allowed_file(
        "gptme/util/tokens.py", repo="gptme/gptme", repo_path_allowlist=allowlist
    )


def test_is_allowed_file_allowlist_does_not_bypass_bot_config() -> None:
    """Allowlist overrides only the sensitive-path heuristic; bot config is never bypassable."""
    # Even if an operator accidentally allowlists a CI workflow, it stays blocked.
    allowlist = {"gptme/gptme": [".github/**"]}
    assert not self_merge_check.is_allowed_file(
        ".github/workflows/ci.yml", repo="gptme/gptme", repo_path_allowlist=allowlist
    )


@pytest.mark.parametrize(
    "path",
    [
        # Instruction prose — the same behaviour-rewriting class as lessons/.
        "skills/financial-advisor/SKILL.md",
        "skills/agent-onboarding/framework-reference.md",
        # Bundled executable helpers: a skill is "lesson + bundled scripts", and
        # splitting the two leaves every skill PR half-mergeable.
        "skills/financial-advisor/calibration.py",
        "skills/code-review-helper/review_helpers.py",
        # Nested helper dirs do NOT start with "scripts/", so is_internal_tooling
        # never covered them.
        "skills/home-assistant/scripts/ha.py",
        "skills/rewrite-soul/agents/openai.yaml",
    ],
)
def test_is_skill_file_recognized(path: str) -> None:
    assert self_merge_check.is_skill_file(path)
    assert self_merge_check.is_allowed_file(path)


@pytest.mark.parametrize(
    "path",
    [
        # Must be anchored at the repo root, not matched as a substring.
        "packages/skills/foo.py",
        "src/skills_registry/loader.py",
        "skillset/foo.py",
        "scripts/skills.py",
    ],
)
def test_is_skill_file_not_recognized(path: str) -> None:
    assert not self_merge_check.is_skill_file(path)


def test_is_internal_tooling_does_not_cover_skill_paths() -> None:
    """Documents WHY is_skill_file is needed: is_internal_tooling is prefix-anchored."""
    assert not self_merge_check.is_internal_tooling(
        "skills/financial-advisor/calibration.py"
    )
    assert not self_merge_check.is_internal_tooling(
        "skills/home-assistant/scripts/ha.py"
    )


@pytest.mark.parametrize(
    "path",
    [
        # The sensitive-path keyword scan runs before the category predicates and
        # still wins — same guard that carries packages/** today.
        "skills/x/deploy_secrets.py",
        "skills/x/auth_token.py",
        "skills/x/credentials.py",
        "skills/deployer/helper.py",
        "skills/x/ssh_config.py",
        "skills/x/k8s_apply.py",
        # Bot/CI config shapes must never ride in under a wholesale-allowed
        # directory, regardless of depth, separator, or case.
        "skills/x/.github/workflows/ci.yml",
        "skills/x/.Github/dependabot.yml",
        "packages/foo/.github/workflows/ci.yml",
        "scripts/.GITHUB/dependabot.yml",
    ],
)
def test_allowed_paths_do_not_bypass_hard_rejects(path: str) -> None:
    assert not self_merge_check.is_allowed_file(path)
    category, reasons = self_merge_check.classify_category([path])
    assert category is None, f"{path} should not be self-mergeable (got {category})"
    assert reasons


def test_skill_allowlist_does_not_bypass_sensitive_paths_in_classify() -> None:
    """A mixed skill PR is disqualified as a whole by one sensitive file."""
    category, reasons = self_merge_check.classify_category(
        [
            "skills/financial-advisor/SKILL.md",
            "skills/financial-advisor/deploy_keys.py",
        ]
    )
    assert category is None, reasons
    assert any("sensitive" in reason.lower() for reason in reasons)


def test_classify_category_skills_only() -> None:
    """PR #1417's exact file set becomes eligible as a skill PR."""
    category, reasons = self_merge_check.classify_category(
        [
            "skills/financial-advisor/SKILL.md",
            "skills/financial-advisor/calibration.py",
            "skills/financial-advisor/tests/test_calibration.py",
        ]
    )
    assert category is not None, reasons
    assert "skill" in category


def test_classify_category_skills_mixed_with_allowed() -> None:
    category, reasons = self_merge_check.classify_category(
        ["skills/foo/SKILL.md", "skills/foo/helper.py", "tasks/my-task.md"]
    )
    assert category is not None, reasons
    assert "skills" in category
    assert "task-metadata" in category


@pytest.mark.parametrize(
    "path",
    [
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.lock",
        "go.sum",
        "packages/mypkg/uv.lock",  # nested (workspace member)
        "subdir/package-lock.json",
    ],
)
def test_is_lockfile_recognized(path: str) -> None:
    assert self_merge_check.is_lockfile(path)


@pytest.mark.parametrize(
    "path",
    [
        "requirements.txt",
        "setup.cfg",
        "pyproject.toml",
        "scripts/lock.py",
        "docs/uv.lock.md",  # not a lockfile
    ],
)
def test_is_lockfile_not_recognized(path: str) -> None:
    assert not self_merge_check.is_lockfile(path)


def test_is_allowed_file_lockfile() -> None:
    assert self_merge_check.is_allowed_file("uv.lock")
    assert self_merge_check.is_allowed_file("poetry.lock")
    assert self_merge_check.is_allowed_file("packages/mypkg/uv.lock")


def test_classify_category_lockfiles_only() -> None:
    """A PR that only touches lockfiles gets the lockfiles-only category."""
    category, reasons = self_merge_check.classify_category(["uv.lock"])
    assert category == "lockfiles-only", reasons

    category, reasons = self_merge_check.classify_category(["uv.lock", "Cargo.lock"])
    assert category == "lockfiles-only", reasons


def test_classify_category_lockfile_mixed_with_allowed() -> None:
    """A PR mixing lockfiles with other allowed files is mixed-allowed."""
    category, reasons = self_merge_check.classify_category(
        ["uv.lock", "scripts/myscript.py"]
    )
    assert category is not None and "lockfiles" in category, reasons

    category, reasons = self_merge_check.classify_category(
        ["uv.lock", "tasks/my-task.md"]
    )
    assert category is not None, reasons
    assert "lockfiles" in category


def test_evaluate_pr_captures_head_sha() -> None:
    """CheckResult.head_sha reflects the headRefOid from fetch_pr."""
    pr_data = {
        "author": {"login": "TimeToBuildBob"},
        "title": "Test PR",
        "url": "https://github.com/gptme/gptme-contrib/pull/999",
        "files": [{"path": "tests/test_example.py"}],
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "isDraft": False,
        "state": "OPEN",
        "reviewDecision": None,
        "headRefOid": "abc123def456",
    }

    with (
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(
            self_merge_check, "_fetch_greptile_review_data", return_value=None
        ),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": True, "unresolved": 0, "total": 1},
        ),
        patch.object(self_merge_check, "greptile_summary_score", return_value=5),
        patch.object(
            self_merge_check, "greptile_summary_reviewed_commit", return_value=None
        ),
        patch.object(
            self_merge_check,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "total": 0, "authors": []},
        ),
    ):
        result = self_merge_check.evaluate_pr(
            "gptme/gptme-contrib",
            999,
            workspace_repos=["gptme/gptme-contrib"],
        )

    assert result.head_sha == "abc123def456"


def _evaluate_with_score_provenance(reviewed_commit, head_sha="abc123def456789a"):
    """Evaluate a has_review PR with score 5 and the given summary provenance."""
    pr_data = {
        "author": {"login": "TimeToBuildBob"},
        "title": "Test PR",
        "url": "https://github.com/gptme/gptme-contrib/pull/999",
        "files": [{"path": "tests/test_example.py"}],
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "isDraft": False,
        "state": "OPEN",
        "reviewDecision": None,
        "headRefOid": head_sha,
        "labels": [],
    }

    with (
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(
            self_merge_check, "_fetch_greptile_review_data", return_value=None
        ),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": True, "unresolved": 0, "total": 1},
        ),
        patch.object(self_merge_check, "greptile_summary_score", return_value=5),
        patch.object(
            self_merge_check,
            "greptile_summary_reviewed_commit",
            return_value=reviewed_commit,
        ),
        patch.object(
            self_merge_check,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "total": 0, "authors": []},
        ),
        patch.object(
            self_merge_check,
            "fetch_ai_review_status",
            return_value={"accepted": True, "detail": "AI review 5/5 at current head"},
        ),
    ):
        return self_merge_check.evaluate_pr(
            "gptme/gptme-contrib",
            999,
            workspace_repos=["gptme/gptme-contrib"],
        )


def test_evaluate_pr_stale_summary_score_falls_through_to_ai_review() -> None:
    """Stale Greptile (reviewed at older head) must not block when AI review is
    accepted — branch 2 of the 3-branch staleness policy: stale Greptile + fresh
    own-AI-review → own reviewer's verdict is the gate."""
    outcome = _evaluate_with_score_provenance("deadbeef" * 5)
    assert outcome.eligible, f"Stale Greptile must not block when AI review accepted; reasons: {outcome.reasons}"
    assert not any("is stale" in r for r in outcome.reasons), outcome.reasons
    assert not any(
        "re-review of head required" in r for r in outcome.reasons
    ), outcome.reasons


def test_evaluate_pr_accepts_score_reviewed_at_head() -> None:
    """Provenance matching the current head adds no stale-score reason."""
    outcome = _evaluate_with_score_provenance("abc123def456789a")
    assert not any("is stale" in r for r in outcome.reasons)


def test_evaluate_pr_score_provenance_absent_fails_open() -> None:
    """No parseable provenance (None) must not block — old summary formats."""
    outcome = _evaluate_with_score_provenance(None)
    assert not any("is stale" in r for r in outcome.reasons)


def test_evaluate_pr_head_sha_empty_when_missing() -> None:
    """head_sha is empty string when fetch_pr returns no headRefOid."""
    pr_data = {
        "author": {"login": "TimeToBuildBob"},
        "title": "Test PR",
        "url": "https://github.com/gptme/gptme-contrib/pull/999",
        "files": [{"path": "tests/test_example.py"}],
        "statusCheckRollup": [],
        "isDraft": False,
        "state": "OPEN",
        "reviewDecision": None,
        # no headRefOid key
    }

    with (
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(
            self_merge_check, "_fetch_greptile_review_data", return_value=None
        ),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": True, "unresolved": 0, "total": 0},
        ),
        patch.object(self_merge_check, "greptile_summary_score", return_value=5),
        patch.object(
            self_merge_check,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "total": 0, "authors": []},
        ),
    ):
        result = self_merge_check.evaluate_pr(
            "gptme/gptme-contrib",
            999,
            workspace_repos=["gptme/gptme-contrib"],
        )

    assert result.head_sha == ""


def test_check_result_head_sha_in_json_output() -> None:
    """head_sha is included in the asdict/JSON serialization."""
    result = self_merge_check.CheckResult(
        eligible=True,
        repo="gptme/gptme-contrib",
        number=999,
        url="https://github.com/gptme/gptme-contrib/pull/999",
        title="Test",
        author="TimeToBuildBob",
        head_sha="deadbeef1234",
    )
    d = dataclasses.asdict(result)
    assert d["head_sha"] == "deadbeef1234"
    assert "head_sha" in json.dumps(d)


# --- spec-like docs are root-only (nested package READMEs are ordinary docs) ---
def test_root_identity_docs_are_spec_like() -> None:
    for name in ("README.md", "ARCHITECTURE.md", "CLAUDE.md", "AGENTS.md", "GOALS.md"):
        assert self_merge_check.is_spec_like_doc(name) is True


def test_nested_package_readme_is_not_spec_like() -> None:
    # A nested package README is package docs, not a top-level spec.
    assert self_merge_check.is_spec_like_doc("packages/gptme-usage/README.md") is False
    assert self_merge_check.is_spec_like_doc("gptme/server/README.md") is False
    assert self_merge_check.is_spec_like_doc("docs/ARCHITECTURE.md") is False


def test_nested_readme_does_not_disqualify_low_risk_pr() -> None:
    # Regression: a clean tooling PR touching a nested package README + code +
    # tests must classify into an allowed category, not be blocked as spec-like.
    paths = [
        "packages/gptme-usage/README.md",
        "packages/gptme-usage/harness-quota.example.toml",
        "packages/gptme-usage/src/gptme_usage/harness_models.py",
        "packages/gptme-usage/tests/test_harness_quota_config.py",
        "scripts/check-quota.py",
    ]
    category, reasons = self_merge_check.classify_category(paths, "gptme/gptme-contrib")
    assert category is not None, reasons
    assert reasons == []


def test_root_readme_still_blocks_self_merge() -> None:
    category, reasons = self_merge_check.classify_category(["README.md"], "gptme/gptme")
    assert category is None
    assert any("spec-like" in r for r in reasons)


def test_dot_slash_prefixed_root_readme_is_spec_like() -> None:
    # GitHub API never produces ./-prefixed paths, but harden against it anyway.
    assert self_merge_check.is_spec_like_doc("./README.md") is True
    assert self_merge_check.is_spec_like_doc("./ARCHITECTURE.md") is True


# --- fetch_pr PR-number mismatch guard ---


def test_fetch_pr_raises_on_number_mismatch() -> None:
    """fetch_pr must raise RuntimeError when the gh response carries a different PR number.

    Root cause: graphql-attribution.sh's on-disk cache had a stride-2 hash that
    caused PR numbers differing by 1 (e.g. 1256 vs 1257) to collide. The cache
    returned stale data with the wrong url/head_sha while the caller-supplied number
    was used for CheckResult.number, producing a silent mismatch.
    """
    # Simulate the shim returning cached data for PR #1256 when #1257 was requested.
    stale_payload = json.dumps(
        {
            "number": 1256,
            "title": "stale cached PR",
            "url": "https://github.com/gptme/gptme-contrib/pull/1256",
            "author": {"login": "TimeToBuildBob"},
            "statusCheckRollup": [],
            "isDraft": False,
            "state": "OPEN",
            "reviewDecision": None,
            "headRefOid": "8a27c142stale",
            "mergeStateStatus": "CLEAN",
        }
    )

    with (
        patch.object(self_merge_check, "run_gh", return_value=stale_payload),
        patch.object(self_merge_check, "_fetch_pr_files", return_value=[]),
    ):
        with pytest.raises(
            RuntimeError, match="PR data mismatch.*1257.*returned.*1256"
        ):
            self_merge_check.fetch_pr("gptme/gptme-contrib", 1257)


def test_fetch_pr_raises_on_missing_number() -> None:
    """fetch_pr must raise when the gh response omits the 'number' field entirely.

    A malformed or truncated cache payload with no 'number' key previously
    passed the guard (None is not None → False) and let stale url/headRefOid
    through. With the is-not-None guard removed, None != requested_number
    triggers the same RuntimeError, ensuring fail-closed behaviour.
    """
    malformed_payload = json.dumps(
        {
            # 'number' key intentionally absent
            "title": "malformed PR",
            "url": "https://github.com/gptme/gptme-contrib/pull/9999",
            "author": {"login": "TimeToBuildBob"},
            "statusCheckRollup": [],
            "isDraft": False,
            "state": "OPEN",
            "reviewDecision": None,
            "headRefOid": "deadbeefmalformed",
            "mergeStateStatus": "CLEAN",
        }
    )

    with (
        patch.object(self_merge_check, "run_gh", return_value=malformed_payload),
        patch.object(self_merge_check, "_fetch_pr_files", return_value=[]),
    ):
        with pytest.raises(RuntimeError, match="PR data mismatch.*1257.*None"):
            self_merge_check.fetch_pr("gptme/gptme-contrib", 1257)


def test_fetch_pr_raises_on_missing_labels_field() -> None:
    """fetch_pr must raise RuntimeError when the gh response omits the 'labels' field.

    The field is explicitly requested via --json, so its absence means an old gh
    CLI version (<2.27) or a cache shim stripping the field. We raise at fetch
    time so callers get an explicit diagnostic instead of a silent eligibility
    block deep in evaluate_pr.

    This is the same fail-explicit pattern used for number-mismatch above.
    """
    payload_missing_labels = json.dumps(
        {
            "number": 1257,
            "title": "test PR",
            "url": "https://github.com/gptme/gptme-contrib/pull/1257",
            "author": {"login": "TimeToBuildBob"},
            "statusCheckRollup": [],
            "isDraft": False,
            "state": "OPEN",
            "reviewDecision": None,
            "headRefOid": "abc123",
            "mergeStateStatus": "CLEAN",
            "baseRefName": "master",
            # 'labels' key intentionally absent — simulates old gh CLI or cache shim
        }
    )

    with (
        patch.object(self_merge_check, "run_gh", return_value=payload_missing_labels),
        patch.object(self_merge_check, "_fetch_pr_files", return_value=[]),
    ):
        with pytest.raises(RuntimeError, match="labels.*field absent from gh response"):
            self_merge_check.fetch_pr("gptme/gptme-contrib", 1257)


def test_is_test_file_spec_ts() -> None:
    """Playwright-style .spec.ts files must be recognised as test files."""
    assert self_merge_check.is_test_file("e2e/demo-real-chat.spec.ts")
    assert self_merge_check.is_test_file("tests/foo.spec.ts")
    assert self_merge_check.is_test_file("src/components/Button.spec.tsx")


def test_is_test_file_spec_js() -> None:
    """Jest/Vitest .spec.js files must be recognised as test files."""
    assert self_merge_check.is_test_file("e2e/login.spec.js")
    assert self_merge_check.is_test_file("src/utils/helper.spec.js")


def test_is_test_file_e2e_directory() -> None:
    """Files under the repository-root e2e/ suite are recognised as test files."""
    assert self_merge_check.is_test_file("e2e/fixtures/data.json")
    assert self_merge_check.is_test_file("src/e2e/login.spec.ts")


def test_is_test_file_spec_plain_name_not_matched() -> None:
    """A file named 'spec.ts' without a leading dot must NOT be a test file."""
    assert not self_merge_check.is_test_file("src/spec.ts")
    assert not self_merge_check.is_test_file("docs/spec.md")


def test_classify_category_e2e_spec_ts_is_test_only() -> None:
    """A PR touching only e2e/*.spec.ts files must be 'test-only' (regression: gptme-cloud#847)."""
    category, reasons = self_merge_check.classify_category(
        ["e2e/demo-real-chat.spec.ts"]
    )
    assert category == "test-only", reasons


# --- `.spec.` must be anchored to JS/TS test extensions, not a bare substring ---
@pytest.mark.parametrize(
    "path",
    [
        "openapi.spec.yaml",
        "api.spec.json",
        "infra.spec.yaml",
        "docs/openapi.spec.yaml",
        "schemas/service.spec.yml",
        "proto/wire.spec.toml",
    ],
)
def test_api_spec_documents_are_not_test_files(path: str) -> None:
    """API/infra specification documents must NOT be treated as tests.

    A bare ``.spec.`` substring marker matched these, letting a PR that only
    changes an OpenAPI/infra spec self-merge as "test-only" without review.
    """
    assert self_merge_check.is_test_file(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "openapi.spec.yaml",
        "api.spec.json",
        "openapi.spec.js",
        "api.spec.ts",
        "infra.spec.mts",
    ],
)
def test_classify_category_api_spec_documents_are_not_test_only(path: str) -> None:
    category, _reasons = self_merge_check.classify_category([path])
    assert category != "test-only"


@pytest.mark.parametrize(
    "path",
    [
        "foo.spec.ts",
        "foo.spec.js",
        "src/components/Button.spec.tsx",
        "e2e/login.spec.jsx",
        "test/setup.spec.mjs",
        "scripts/legacy.spec.cjs",
    ],
)
def test_js_ts_spec_files_are_still_test_files(path: str) -> None:
    """The feature this PR adds must keep working for real JS/TS spec files."""
    assert self_merge_check.is_test_file(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "foo.spec.mts",
        "foo.spec.cts",
        "src/lib/loader.spec.mts",
        "packages/api/legacy.spec.cts",
        "foo.SPEC.MTS",
    ],
)
def test_typescript_module_spec_files_are_test_files(path: str) -> None:
    """``.mts``/``.cts`` are the TS counterparts of the already-accepted
    ``.mjs``/``.cjs``. Omitting them made ``foo.spec.mts`` a false negative for
    ``type: module`` TypeScript repos, an internal inconsistency in the marker set.
    """
    assert self_merge_check.is_test_file(path) is True


@pytest.mark.parametrize(
    "path", ["foo.spec.ts", "foo.spec.js", "foo.spec.mts", "foo.spec.cts"]
)
def test_classify_category_js_ts_spec_files_are_test_only(path: str) -> None:
    category, reasons = self_merge_check.classify_category([path])
    assert category == "test-only", reasons


# --- `e2e` must be a path COMPONENT, not a bare substring ---
@pytest.mark.parametrize(
    "path",
    [
        "src/ee2e/bar.py",
        "src/foo-e2e/bar.py",
        "packages/pre2e2e/main.go",
        "e2estuff/thing.py",
    ],
)
def test_e2e_substring_lookalikes_are_not_test_files(path: str) -> None:
    """`e2e/` as a raw substring matched directories merely *containing* "e2e"."""
    assert self_merge_check.is_test_file(path) is False


@pytest.mark.parametrize("path", ["src/ee2e/bar.py", "src/foo-e2e/bar.py"])
def test_classify_category_e2e_lookalikes_are_not_test_only(path: str) -> None:
    category, _reasons = self_merge_check.classify_category([path])
    assert category != "test-only"


@pytest.mark.parametrize(
    "path",
    [
        "e2e/foo.ts",
        "e2e/fixtures/data.json",
        "./e2e/foo.ts",
    ],
)
def test_real_e2e_directories_are_still_test_files(path: str) -> None:
    assert self_merge_check.is_test_file(path) is True


@pytest.mark.parametrize("path", ["e2e/foo.ts", "./e2e/foo.ts"])
def test_classify_category_real_e2e_dirs_are_test_only(path: str) -> None:
    category, reasons = self_merge_check.classify_category([path])
    assert category == "test-only", reasons


@pytest.mark.parametrize("path", ["src/e2e/helpers.ts", "packages/x/e2e/runner.py"])
def test_nested_e2e_directories_are_not_implicitly_test_files(path: str) -> None:
    """Only the conventional repository-root e2e/ suite is trusted.

    A nested ``e2e`` package can hold shipped code, so trusting every ``e2e``
    component would make the gate fail open. Files there still qualify through
    an explicit test filename such as ``*.spec.ts``.
    """
    assert self_merge_check.is_test_file(path) is False


# --- environment-defining files are never test-only, wherever they live ---
@pytest.mark.parametrize(
    "path",
    [
        # The four that shipped as self-mergeable when `e2e` was added.
        "e2e/Dockerfile",
        "e2e/Containerfile",
        "e2e/docker-compose.yml",
        "e2e/docker_compose.yml",
        "e2e/playwright.config.ts",
        "e2e/global-setup.ts",
        "e2e/global_setup.ts",
        "e2e/globalSetup.ts",
        "e2e/global-teardown.ts",
        "e2e/global_teardown.ts",
        "e2e/globalTeardown.ts",
        "e2e/vitest.workspace.ts",
        "e2e/vitest.workspace.mts",
        "e2e/.env",
        "e2e/.env.local",
        "e2e/start-server.js",
        "e2e/tsconfig.json",
        "e2e/requirements.txt",
        # Same hole, pre-existing, under `tests/`.
        "tests/Dockerfile",
        "tests/Makefile",
        "tests/package.json",
        "tests/run.sh",
        # Variants.
        "e2e/Dockerfile.ci",
        "e2e/compose.yaml",
        "e2e/global-teardown.ts",
        "tests/vitest.config.mts",
    ],
)
def test_environment_defining_files_are_never_test_only(path: str) -> None:
    """A Dockerfile under e2e/ defines what CI executes — not "just a test".

    Classifying these as test-only let an environment change self-merge with no
    human review, in a gate whose own comment says it must fail closed.
    """
    assert self_merge_check.is_test_file(path) is False
    category, reasons = self_merge_check.classify_category([path])
    assert category != "test-only", reasons


@pytest.mark.parametrize(
    "path",
    [
        "e2e/login.spec.ts",
        "e2e/fixtures/data.json",
        "tests/fixtures/setup.sql",
        "tests/data/config.json",
        "tests/helpers/client-config.ts",
        "tests/mock-server.ts",
        "tests/helpers/util.py",
        "tests/conftest.py",
    ],
)
def test_ordinary_test_files_and_fixtures_still_qualify(path: str) -> None:
    """The exclusion must not swallow genuine test code or test data."""
    assert self_merge_check.is_test_file(path) is True


# --- pre-existing substring markers (`tests/`, `test_`) had the same weakness ---
@pytest.mark.parametrize(
    "path",
    [
        # `tests/` as a raw substring matched any directory ENDING in "tests"
        "src/contests/foo.py",
        "src/protests/view.py",
        "app/latests/handler.rb",
        # `test_` as a raw substring matched any filename CONTAINING it
        "src/protest_ui.py",
        "src/latest_thing.py",
        "lib/greatest_hits.go",
    ],
)
def test_legacy_substring_markers_do_not_match_lookalikes(path: str) -> None:
    """Non-test source must not be classifiable as a test file.

    These are ordinary source files that the raw-substring markers wrongly
    claimed, which would have let them self-merge as "test-only".
    """
    assert self_merge_check.is_test_file(path) is False


@pytest.mark.parametrize("path", ["src/contests/foo.py", "src/protest_ui.py"])
def test_classify_category_legacy_lookalikes_are_not_test_only(path: str) -> None:
    category, _reasons = self_merge_check.classify_category([path])
    assert category != "test-only"


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_foo.py",
        "tests/helpers/fixtures.json",
        "packages/x/tests/test_bar.py",
        "src/test_module.py",
        "pkg/handler_test.go",
        "src/utils/date.test.ts",
        "./tests/test_root.py",
    ],
)
def test_genuine_test_paths_still_match(path: str) -> None:
    assert self_merge_check.is_test_file(path) is True


# --- gptme/gptme core: diff-shape eligibility, not path category ---
#
# Erik (2026-09-07), on gptme#3620's gate message: "path alone doesn't feel
# like a good eligibility gate, at least not for this PR". Corpus fixtures
# below capture real file lists (`gh pr view --json title,body,files`, 3
# calls total) for the PRs named in
# tasks/gptme-core-self-merge-diff-shape-not-path-category.md; patch text is
# synthesized to match each PR's documented shape (not re-fetched — the
# detectors only need to see *some* added line of the relevant shape).


def test_gptme_core_cache_fix_no_route_reasons() -> None:
    """gptme#3620: cache fix in gptme/util/reduce.py + tests, clean review.

    Modifies an existing core-internal module and adds tests only — no new
    CLI/config/module/docs surface, no unlinked feat, nothing sensitive.
    Must produce zero route-to-Erik reasons (diff-shape eligible).
    """
    file_shapes = [
        {
            "path": "gptme/util/reduce.py",
            "status": "modified",
            "patch": (
                "@@ -10,6 +10,10 @@\n"
                "+_CACHE: dict[str, Message] = {}\n"
                "+\n"
                "+def _cache_key(model: str, msgs: list[Message]) -> str:\n"
                "+    return hashlib.sha256(...).hexdigest()\n"
            ),
        },
        {
            "path": "tests/conftest.py",
            "status": "modified",
            "patch": (
                "@@ -1,3 +1,22 @@\n"
                "+@pytest.fixture(autouse=True)\n"
                "+def _clear_reduce_cache():\n"
                "+    yield\n"
            ),
        },
        {
            "path": "tests/test_reduce.py",
            "status": "added",
            "patch": (
                "@@ -0,0 +1,296 @@\n"
                "+def test_proactive_summarize_cache_hit_skips_llm():\n"
                "+    pass\n"
            ),
        },
    ]
    reasons = self_merge_check.gptme_core_route_to_erik_reasons(
        "fix(reduce): cache proactive_summarize_log result by middle-message content hash",
        "Phase 4.1 of the append-only request construction audit. "
        "Phases 2+3 landed in #3616.",
        file_shapes,
    )
    assert reasons == []


def test_gptme_core_new_hook_module_and_config_routes() -> None:
    """gptme#3695: new gptme/hooks/guardrails.py hook module + config surface.

    A wholly new module two levels under gptme/, plus a new env-gated config
    knob declared in gptme/config/ — must route to Erik on the new-module (and,
    when the config field is present, config) reason.
    """
    file_shapes = [
        {
            "path": "gptme/hooks/__init__.py",
            "status": "modified",
            "patch": (
                "@@ -1,3 +1,8 @@\n" "+register_hook(guardrails_hook, priority=200)\n"
            ),
        },
        {
            "path": "gptme/hooks/guardrails.py",
            "status": "added",
            "patch": (
                "@@ -0,0 +1,677 @@\n" "+def guardrails_hook(...):\n" "+    pass\n"
            ),
        },
        {
            "path": "gptme/config/models.py",
            "status": "modified",
            "patch": (
                "@@ -260,6 +260,9 @@ class HooksConfig:\n"
                '+    guardrails_mode: str = field(default="shadow")\n'
            ),
        },
        {
            "path": "tests/test_guardrails_policy.py",
            "status": "added",
            "patch": ("@@ -0,0 +1,43 @@\n+def test_shell_policy(): pass\n"),
        },
    ]
    reasons = self_merge_check.gptme_core_route_to_erik_reasons(
        "feat(hooks): add guardrails policy hook",
        "Resolves the three design questions from RFC #3598 ... Closes #3598",
        file_shapes,
    )
    assert any("New module under gptme/" in r and "guardrails.py" in r for r in reasons)
    assert any("New config schema key" in r and "models.py" in r for r in reasons)
    # This PR *does* link an issue (#3598), so the unlinked-feat reason must
    # not also fire — only the new-module/config reasons should.
    assert not any("no #issue reference" in r for r in reasons)


def test_gptme_core_new_top_level_module_routes() -> None:
    """gptme#3706: new top-level gptme/error_hintkit.py module.

    A bare new top-level module (added file, one path segment under gptme/)
    must route to Erik on the new-module reason.
    """
    file_shapes = [
        {
            "path": "gptme/cli/main.py",
            "status": "modified",
            "patch": ("@@ -10,6 +10,7 @@\n+sys.excepthook = hintkit_excepthook\n"),
        },
        {
            "path": "gptme/error_hintkit.py",
            "status": "added",
            "patch": ("@@ -0,0 +1,261 @@\n+HINTKIT_ENABLED = True\n"),
        },
        {
            "path": "tests/test_error_hintkit.py",
            "status": "added",
            "patch": ("@@ -0,0 +1,250 @@\n+def test_hint(): pass\n"),
        },
        {
            "path": "tests/test_cli_fatal_error_envelope.py",
            "status": "added",
            "patch": ("@@ -0,0 +1,89 @@\n+def test_envelope(): pass\n"),
        },
    ]
    reasons = self_merge_check.gptme_core_route_to_erik_reasons(
        "feat(cli): add Error-HintKit hints",
        "Part of Error-HintKit M1 from Bob's local task "
        "error-hintkit-m1-registry-and-cli-adapter.",
        file_shapes,
    )
    assert any(
        "New module under gptme/" in r and "error_hintkit.py" in r for r in reasons
    )
    # feat: title with genuinely no #issue reference anywhere — must ALSO fire.
    assert any("no #issue reference" in r for r in reasons)


def test_gptme_core_feat_title_without_issue_routes() -> None:
    """A `feat:` PR touching only ordinary internal files but with no linked
    issue anywhere in title or body must still route (auto-merge-analysis §3
    cluster 5: unmotivated/low-value features are exactly what Erik closes)."""
    file_shapes = [
        {
            "path": "gptme/tools/browser.py",
            "status": "modified",
            "patch": ("@@ -1,3 +1,9 @@\n+def new_helper():\n+    pass\n"),
        },
        {
            "path": "tests/test_browser.py",
            "status": "modified",
            "patch": ("@@ -1,3 +1,7 @@\n+def test_new_helper(): pass\n"),
        },
    ]
    reasons = self_merge_check.gptme_core_route_to_erik_reasons(
        "feat: add a configurable retry delay to the browser tool",
        "No design doc, just seemed useful.",
        file_shapes,
    )
    assert len(reasons) == 1
    assert "no #issue reference" in reasons[0]


def test_gptme_core_new_click_option_routes() -> None:
    file_shapes = [
        {
            "path": "gptme/cli/main.py",
            "status": "modified",
            "patch": (
                "@@ -540,6 +540,11 @@\n"
                '+@click.option("--no-verify", is_flag=True, help="Skip verification")\n'
            ),
        },
    ]
    reasons = self_merge_check.gptme_core_route_to_erik_reasons(
        "fix(cli): add --no-verify escape hatch", "", file_shapes
    )
    assert any("New CLI surface" in r for r in reasons)


def test_gptme_core_new_cli_subcommand_file_routes() -> None:
    file_shapes = [
        {
            "path": "gptme/cli/cmd_replay.py",
            "status": "added",
            "patch": (
                "@@ -0,0 +1,40 @@\n+@click.command()\n+def cmd_replay():\n+    pass\n"
            ),
        },
    ]
    reasons = self_merge_check.gptme_core_route_to_erik_reasons(
        "fix(cli): add replay subcommand", "", file_shapes
    )
    assert any("New CLI surface" in r and "cmd_replay.py" in r for r in reasons)


def test_gptme_core_new_docs_page_routes() -> None:
    file_shapes = [
        {
            "path": "docs/replay.rst",
            "status": "added",
            "patch": ("@@ -0,0 +1,20 @@\n+Replay\n+======\n"),
        },
    ]
    reasons = self_merge_check.gptme_core_route_to_erik_reasons(
        "fix(docs): document replay", "", file_shapes
    )
    assert any("New docs page" in r and "replay.rst" in r for r in reasons)


def test_gptme_core_llm_server_provider_paths_route() -> None:
    file_shapes = [
        {
            "path": "gptme/llm/llm_anthropic.py",
            "status": "modified",
            "patch": ("@@ -1,3 +1,6 @@\n+def _retry(): pass\n"),
        },
    ]
    reasons = self_merge_check.gptme_core_route_to_erik_reasons(
        "fix(llm): retry on 529", "", file_shapes
    )
    assert any(
        "gptme/llm/, gptme/server/, auth, or provider routing" in r for r in reasons
    )


def test_gptme_core_modified_existing_module_is_not_new() -> None:
    """A modified (not added) file under gptme/ must not trip the new-module
    detector — only genuinely new files count."""
    file_shapes = [
        {
            "path": "gptme/tools/browser.py",
            "status": "modified",
            "patch": ("@@ -1,3 +1,5 @@\n+x = 1\n"),
        },
    ]
    reasons = self_merge_check.gptme_core_route_to_erik_reasons(
        "fix(tools): tighten browser timeout", "", file_shapes
    )
    assert reasons == []


def _evaluate_gptme_core(
    files: list[dict[str, Any]],
    file_shapes: list[dict[str, Any]],
    *,
    title: str = "fix(reduce): cache result",
    body: str = "",
) -> Any:
    pr_data = _make_clean_pr_data(
        title=title,
        body=body,
        files=files,
        url="https://github.com/gptme/gptme/pull/999",
    )
    with (
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(
            self_merge_check, "_fetch_greptile_review_data", return_value=None
        ),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": False, "unresolved": 0, "total": 0},
        ),
        patch.object(self_merge_check, "greptile_summary_score", return_value=None),
        patch.object(
            self_merge_check,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "total": 0, "authors": []},
        ),
        patch.object(
            self_merge_check,
            "fetch_ai_review_status",
            return_value={"accepted": True, "detail": "AI review 5/5 at current head"},
        ),
        patch.object(
            self_merge_check, "_fetch_pr_file_shapes", return_value=file_shapes
        ),
    ):
        return self_merge_check.evaluate_pr(
            "gptme/gptme",
            999,
            workspace_repos=["gptme/gptme"],
        )


def test_evaluate_pr_gptme_core_cache_fix_is_eligible() -> None:
    """gptme#3620 end-to-end: CI green, AI review clean, no route-to-Erik
    shape → eligible regardless of gptme/util/reduce.py's directory."""
    files = [
        {"path": "gptme/util/reduce.py"},
        {"path": "tests/conftest.py"},
        {"path": "tests/test_reduce.py"},
    ]
    file_shapes = [
        {
            "path": "gptme/util/reduce.py",
            "status": "modified",
            "patch": "@@ -1,1 +1,2 @@\n+x = 1\n",
        },
        {
            "path": "tests/conftest.py",
            "status": "modified",
            "patch": "@@ -1,1 +1,2 @@\n+y = 1\n",
        },
        {
            "path": "tests/test_reduce.py",
            "status": "added",
            "patch": "@@ -0,0 +1,2 @@\n+def test_x(): pass\n",
        },
    ]
    result = _evaluate_gptme_core(
        files,
        file_shapes,
        title="fix(reduce): cache proactive_summarize_log result by middle-message content hash",
        body="Phase 4.1 of the append-only request construction audit. Phases 2+3 landed in #3616.",
    )
    assert result.eligible, result.reasons
    assert result.category == "gptme-core-diff-shape-eligible"


def test_evaluate_pr_gptme_core_new_hook_module_routes_to_erik() -> None:
    """gptme#3695 end-to-end: new hook module + config surface → NOT eligible,
    even with CI green and AI review clean."""
    files = [
        {"path": "gptme/hooks/__init__.py"},
        {"path": "gptme/hooks/guardrails.py"},
        {"path": "gptme/config/models.py"},
        {"path": "tests/test_guardrails_policy.py"},
    ]
    file_shapes = [
        {
            "path": "gptme/hooks/__init__.py",
            "status": "modified",
            "patch": "@@ -1,1 +1,2 @@\n+register_hook(guardrails_hook)\n",
        },
        {
            "path": "gptme/hooks/guardrails.py",
            "status": "added",
            "patch": "@@ -0,0 +1,2 @@\n+def guardrails_hook(): pass\n",
        },
        {
            "path": "gptme/config/models.py",
            "status": "modified",
            "patch": '@@ -260,1 +260,2 @@\n+    guardrails_mode: str = field(default="shadow")\n',
        },
        {
            "path": "tests/test_guardrails_policy.py",
            "status": "added",
            "patch": "@@ -0,0 +1,2 @@\n+def test_x(): pass\n",
        },
    ]
    result = _evaluate_gptme_core(
        files,
        file_shapes,
        title="feat(hooks): add guardrails policy hook",
        body="Resolves the three design questions from RFC #3598 ... Closes #3598",
    )
    assert not result.eligible
    assert any("New module under gptme/" in r for r in result.reasons)


def test_evaluate_pr_gptme_core_new_top_level_module_routes_to_erik() -> None:
    """gptme#3706 end-to-end: new top-level gptme/error_hintkit.py module."""
    files = [
        {"path": "gptme/cli/main.py"},
        {"path": "gptme/error_hintkit.py"},
        {"path": "tests/test_error_hintkit.py"},
        {"path": "tests/test_cli_fatal_error_envelope.py"},
    ]
    file_shapes = [
        {
            "path": "gptme/cli/main.py",
            "status": "modified",
            "patch": "@@ -1,1 +1,2 @@\n+x = 1\n",
        },
        {
            "path": "gptme/error_hintkit.py",
            "status": "added",
            "patch": "@@ -0,0 +1,2 @@\n+HINTKIT_ENABLED = True\n",
        },
        {
            "path": "tests/test_error_hintkit.py",
            "status": "added",
            "patch": "@@ -0,0 +1,2 @@\n+def test_x(): pass\n",
        },
        {
            "path": "tests/test_cli_fatal_error_envelope.py",
            "status": "added",
            "patch": "@@ -0,0 +1,2 @@\n+def test_y(): pass\n",
        },
    ]
    result = _evaluate_gptme_core(
        files,
        file_shapes,
        title="feat(cli): add Error-HintKit hints",
        body="Part of Error-HintKit M1 from Bob's local task "
        "error-hintkit-m1-registry-and-cli-adapter.",
    )
    assert not result.eligible
    assert any("New module under gptme/" in r for r in result.reasons)
    assert any("no #issue reference" in r for r in result.reasons)


def test_evaluate_pr_gptme_core_feat_without_issue_routes_to_erik() -> None:
    """A synthetic `feat:` PR with no linked issue must route, even when every
    file it touches is otherwise an ordinary internal module."""
    files = [
        {"path": "gptme/tools/browser.py"},
        {"path": "tests/test_browser.py"},
    ]
    file_shapes = [
        {
            "path": "gptme/tools/browser.py",
            "status": "modified",
            "patch": "@@ -1,1 +1,2 @@\n+def new_helper(): pass\n",
        },
        {
            "path": "tests/test_browser.py",
            "status": "modified",
            "patch": "@@ -1,1 +1,2 @@\n+def test_x(): pass\n",
        },
    ]
    result = _evaluate_gptme_core(
        files,
        file_shapes,
        title="feat: add a configurable retry delay to the browser tool",
        body="No design doc, just seemed useful.",
    )
    assert not result.eligible
    assert any("no #issue reference" in r for r in result.reasons)
    assert result.category is None


def test_evaluate_pr_gptme_core_sensitive_path_still_blocks() -> None:
    """The generic sensitive-path hard stop still applies to gptme/gptme —
    dropping the category-allowlist requirement does not touch it."""
    files = [{"path": "gptme/oauth/tokens.py"}]
    file_shapes = [
        {
            "path": "gptme/oauth/tokens.py",
            "status": "modified",
            "patch": "@@ -1,1 +1,2 @@\n+x = 1\n",
        }
    ]
    result = _evaluate_gptme_core(files, file_shapes, title="fix(oauth): rotate token")
    assert not result.eligible
    assert any("sensitive" in r.lower() for r in result.reasons)


def test_evaluate_pr_other_repo_category_allowlist_unchanged() -> None:
    """gptme/gptme-contrib still uses classify_category for remaining files.

    A top-level FILE (no slash) is not a new top-level directory, so the
    contrib placement rule does not fire; the old path-category allowlist
    is what disqualifies it. gptme-core's patch-text fetch must not run.
    """
    files = [{"path": "orphan_module.py"}]
    pr_data = _make_clean_pr_data(files=files)
    with (
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(
            self_merge_check, "_fetch_greptile_review_data", return_value=None
        ),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": False, "unresolved": 0, "total": 0},
        ),
        patch.object(self_merge_check, "greptile_summary_score", return_value=None),
        patch.object(
            self_merge_check,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "total": 0, "authors": []},
        ),
        patch.object(
            self_merge_check,
            "fetch_ai_review_status",
            return_value={"accepted": True, "detail": "AI review 5/5 at current head"},
        ),
        patch.object(self_merge_check, "_fetch_pr_file_shapes") as mock_shapes,
    ):
        result = self_merge_check.evaluate_pr(
            "gptme/gptme-contrib",
            999,
            workspace_repos=["gptme/gptme-contrib"],
        )
    # orphan_module.py is not in any allowed category for contrib — the
    # path-category allowlist still applies to remaining (non-placement) files.
    assert not result.eligible
    assert any(
        "Files not in any allowed self-merge category" in r for r in result.reasons
    )
    mock_shapes.assert_not_called()

# --- gptme/gptme-contrib: placement rule + segment-matched sensitive paths +
# spec-doc waiver ---
#
# Erik (2026-09-07 corpus, auto-merge-analysis.md §3 cluster 2 / §5 T2).
# Fixtures below are real file lists (`gh pr view --json files`, 9 calls
# total across this task) for the corpus PRs named in
# tasks/contrib-self-merge-placement-rule-and-segment-paths.md.


def test_contrib_new_top_level_dir_cookbook_routes() -> None:
    """contrib#1325 (CLOSED): a brand-new top-level cookbook/ directory.

    Erik: "better placed in docs (core)" — a new top-level dir is exactly
    the placement decision classify_category's path-category allowlist
    cannot see (scripts/build-cookbook.py alone would be plain
    internal-tooling)."""
    files = [
        "cookbook/.gitignore",
        "cookbook/01-tool-use-basics.md",
        "cookbook/README.md",
        "scripts/build-cookbook.py",
    ]
    category, reasons = self_merge_check.contrib_classify_category(
        files, "gptme/gptme-contrib"
    )
    assert category is None
    assert any("New top-level directory" in r and "cookbook" in r for r in reasons)


def test_contrib_new_top_level_dir_knowledge_routes() -> None:
    """contrib#1428 (CLOSED): a brand-new top-level knowledge/ directory.

    Erik, verbatim: "the `knowledge` top-level directory shouldn't exist"."""
    files = [
        "knowledge/decks/agentic-presentation-demo.json",
        "scripts/generate-presentation-html.py",
        "skills/agentic-presentation/SKILL.md",
        "tests/test_generate_presentation_html.py",
    ]
    category, reasons = self_merge_check.contrib_classify_category(
        files, "gptme/gptme-contrib"
    )
    assert category is None
    assert any("New top-level directory" in r and "knowledge" in r for r in reasons)


def test_contrib_basename_collision_wisdom_mcp_routes() -> None:
    """contrib#1219 (MERGED): gptme-wisdom-mcp's indexer.py/mcp_server.py
    duplicate basenames that already live under packages/gptme-rag/src/ (and
    packages/gptme-wisdom/src/). Erik, verbatim: "please don't re-implement
    prior art (gptme-rag)". classify_category alone would pass this PR —
    packages/** is already-allowed internal-tooling; the collision is a diff
    SHAPE, not a path category."""
    files = [
        "mypy.ini",
        "packages/gptme-wisdom-mcp/Makefile",
        "packages/gptme-wisdom-mcp/README.md",
        "packages/gptme-wisdom-mcp/pyproject.toml",
        "packages/gptme-wisdom-mcp/src/gptme_wisdom_mcp/__init__.py",
        "packages/gptme-wisdom-mcp/src/gptme_wisdom_mcp/indexer.py",
        "packages/gptme-wisdom-mcp/src/gptme_wisdom_mcp/mcp_server.py",
        "packages/gptme-wisdom-mcp/src/gptme_wisdom_mcp/parsers.py",
        "packages/gptme-wisdom-mcp/tests/test_indexer.py",
        "uv.lock",
    ]
    category, reasons = self_merge_check.contrib_classify_category(
        files, "gptme/gptme-contrib"
    )
    assert category is None
    collision_reasons = [r for r in reasons if "basename collides" in r]
    assert collision_reasons, reasons
    assert "indexer.py" in collision_reasons[0]
    assert "mcp_server.py" in collision_reasons[0]
    assert "gptme-rag" in collision_reasons[0]


def test_contrib_new_package_alone_routes() -> None:
    """A brand-new packages/<name>/ that does NOT collide with any existing
    basename must still route on the new-package reason alone (isolates the
    new-package rule from the basename-collision rule above)."""
    files = [
        "packages/gptme-totally-novel-thing/pyproject.toml",
        "packages/gptme-totally-novel-thing/src/gptme_totally_novel_thing/__init__.py",
        "packages/gptme-totally-novel-thing/src/gptme_totally_novel_thing/unique_widget.py",
    ]
    category, reasons = self_merge_check.contrib_classify_category(
        files, "gptme/gptme-contrib"
    )
    assert category is None
    assert any(
        "New packages/<name>/" in r and "gptme-totally-novel-thing" in r
        for r in reasons
    )


def test_contrib_state_writes_route() -> None:
    """Writes under state/ route to Erik. Erik, verbatim (contrib#1250):
    "state belongs in brain, not contrib"."""
    files = ["state/some-new-ledger.jsonl", "scripts/emit-ledger.py"]
    category, reasons = self_merge_check.contrib_classify_category(
        files, "gptme/gptme-contrib"
    )
    assert category is None
    assert any("Writes under state/" in r for r in reasons)


def test_contrib_loose_file_directly_under_packages_is_not_a_new_package() -> None:
    """A file placed directly at packages/<file>, with no nested directory
    (e.g. packages/README.md), must not be misread as a new package named
    "README.md" — regression guard for the len(parts) >= 3 requirement."""
    files = ["packages/README.md"]
    new_pkgs = self_merge_check._contrib_new_packages(files)
    assert new_pkgs == []


def test_contrib_merged_untouched_runloops_fix_is_clean() -> None:
    """contrib#1621 (merged by Erik untouched): an ordinary fix to an
    existing package. No placement reason should fire — this is exactly the
    class of PR the whole T2 policy exists to stop routing unnecessarily."""
    files = [
        "packages/gptme-runloops/src/gptme_runloops/run_item.py",
        "packages/gptme-runloops/src/gptme_runloops/worker_records.py",
        "packages/gptme-runloops/tests/test_worker_records_voice_postcall.py",
    ]
    category, reasons = self_merge_check.contrib_classify_category(
        files, "gptme/gptme-contrib"
    )
    assert category == "internal-tooling", reasons
    assert reasons == []


def test_contrib_merged_untouched_voice_vision_fix_is_clean() -> None:
    """contrib#1617 (merged by Erik untouched): same shape, different
    package."""
    files = [
        "packages/gptme-voice/src/gptme_voice/vision.py",
        "packages/gptme-voice/tests/test_vision.py",
    ]
    category, reasons = self_merge_check.contrib_classify_category(
        files, "gptme/gptme-contrib"
    )
    assert category == "internal-tooling", reasons
    assert reasons == []


def test_contrib_spec_doc_waived_when_paired_with_package_impl() -> None:
    """contrib#1576 (merged by Erik untouched, 73 identical refusals before
    that): root README.md registers a package the SAME PR implements
    end-to-end. Mirror table (auto-merge-analysis §5): 'Yes when the doc
    lives inside a package the same PR implements.'"""
    files = [
        "README.md",
        "packages/gptme-body-protocol/README.md",
        "packages/gptme-body-protocol/src/gptme_body_protocol/__init__.py",
        "packages/gptme-body-protocol/tests/test_protocol.py",
    ]
    category, reasons = self_merge_check.contrib_classify_category(
        files, "gptme/gptme-contrib"
    )
    assert category is not None, reasons
    assert category.endswith("+spec-doc-waived")
    assert not any("spec-like" in r.lower() for r in reasons)


def test_contrib_spec_doc_not_waived_when_doc_only() -> None:
    """A doc-only PR touching a root SPEC_LIKE_DOCS file with nothing else in
    the diff is NOT paired with package implementation work — must still
    route to Erik, same as before this change."""
    files = ["README.md"]
    category, reasons = self_merge_check.contrib_classify_category(
        files, "gptme/gptme-contrib"
    )
    assert category is None
    assert any("spec-like" in r.lower() for r in reasons)


@pytest.mark.parametrize(
    "path",
    [
        "secrets_test.py",
        "tests/secrets_test.py",
        "test_secrets.py",
        "packages/foo/tests/test_secrets.py",
        "tests/deploy_test.py",
    ],
)
def test_is_sensitive_path_segment_not_substring_for_test_files(path: str) -> None:
    """A test file's OWN name incidentally containing a sensitive word (e.g.
    'secrets_test.py' — a test file ABOUT secrets handling, not a file that
    HOLDS a secret) must not trip the keyword scan. Segment/path-shape
    matching, not substring — task
    tasks/contrib-self-merge-placement-rule-and-segment-paths.md item 2."""
    assert self_merge_check.is_test_file(path) is True
    assert self_merge_check.is_sensitive_path(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "secrets/id_rsa",
        "secrets/prod.env",
        "scripts/deploy-prod.sh",
        "packages/foo/authToken.py",
    ],
)
def test_is_sensitive_path_still_blocks_real_sensitive_paths(path: str) -> None:
    """The is_test_file() carve-out only applies to test files — a real
    secret-bearing or sensitive path is unaffected."""
    assert self_merge_check.is_sensitive_path(path) is True


def test_contrib_agent_literal_in_non_test_code_routes() -> None:
    """contrib#1088-shaped case: an agent-name literal added to non-test
    code. Erik, verbatim: "Bob shouldn't have his configuration in
    gptme-contrib"."""
    file_shapes = [
        {
            "path": "packages/gptme-subscription/src/gptme_subscription/harness_models.py",
            "status": "modified",
            "patch": (
                "@@ -10,6 +10,8 @@\n"
                "+BOB_QUOTA_OVERRIDE_USD = 40.0  # Bob-specific quota bump\n"
            ),
        },
    ]
    reasons = self_merge_check.contrib_agent_literal_reasons(file_shapes)
    assert any('"bob"' in r for r in reasons)


def test_contrib_agent_literal_in_test_code_is_excluded() -> None:
    """The same literal added to a test fixture (e.g. testing attribution
    logic across agent names) must not route — only non-test code counts."""
    file_shapes = [
        {
            "path": "packages/gptme-sessions/tests/test_attribution.py",
            "status": "modified",
            "patch": ("@@ -1,3 +1,5 @@\n+def test_bob_attribution():\n+    pass\n"),
        },
    ]
    reasons = self_merge_check.contrib_agent_literal_reasons(file_shapes)
    assert reasons == []


def test_contrib_agent_literal_word_boundary_no_false_positive() -> None:
    """ "erik" as a bound token must fire; a name that merely CONTAINS the
    substring ("erikson") must not — word-boundary, not substring, matching."""
    file_shapes = [
        {
            "path": "packages/gptme-example/src/gptme_example/scoring.py",
            "status": "modified",
            "patch": "@@ -1,3 +1,4 @@\n+ERIKSON_COEFFICIENT = 0.7\n",
        },
    ]
    reasons = self_merge_check.contrib_agent_literal_reasons(file_shapes)
    assert reasons == []


def _evaluate_contrib(
    files: list[dict[str, Any]],
    file_shapes: list[dict[str, Any]],
    *,
    title: str = "fix(gptme-runloops): tighten worker record timeout",
) -> Any:
    pr_data = _make_clean_pr_data(
        title=title,
        files=files,
        url="https://github.com/gptme/gptme-contrib/pull/999",
    )
    with (
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(
            self_merge_check, "_fetch_greptile_review_data", return_value=None
        ),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": False, "unresolved": 0, "total": 0},
        ),
        patch.object(self_merge_check, "greptile_summary_score", return_value=None),
        patch.object(
            self_merge_check,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "total": 0, "authors": []},
        ),
        patch.object(
            self_merge_check,
            "fetch_ai_review_status",
            return_value={"accepted": True, "detail": "AI review 5/5 at current head"},
        ),
        patch.object(
            self_merge_check, "_fetch_contrib_pr_file_shapes", return_value=file_shapes
        ),
    ):
        return self_merge_check.evaluate_pr(
            "gptme/gptme-contrib",
            999,
            workspace_repos=["gptme/gptme-contrib"],
        )


def test_evaluate_pr_contrib_new_top_level_dir_not_eligible() -> None:
    """End-to-end: CI green, AI review clean, but a new top-level dir in the
    diff → NOT eligible, and _fetch_contrib_pr_file_shapes must not even be
    called (the cheaper path-only placement check already disqualified it)."""
    files = [
        {"path": "cookbook/01-tool-use-basics.md"},
        {"path": "scripts/build-cookbook.py"},
    ]
    with patch.object(self_merge_check, "_fetch_contrib_pr_file_shapes") as mock_shapes:
        mock_shapes.return_value = []
        result = _evaluate_contrib(files, [], title="feat(cookbook): add cookbook")
    assert not result.eligible
    assert any("New top-level directory" in r for r in result.reasons)
    mock_shapes.assert_not_called()


def test_evaluate_pr_contrib_agent_literal_end_to_end_not_eligible() -> None:
    """End-to-end: an ordinary-looking internal-tooling diff (so the
    path-only placement checks pass) with an agent-name literal added in the
    patch text → NOT eligible."""
    files = [{"path": "packages/gptme-runloops/src/gptme_runloops/scoring_helper.py"}]
    file_shapes = [
        {
            "path": "packages/gptme-runloops/src/gptme_runloops/scoring_helper.py",
            "status": "modified",
            "patch": "@@ -1,3 +1,4 @@\n+BOB_SPECIFIC_WEIGHT = 1.5\n",
        }
    ]
    result = _evaluate_contrib(files, file_shapes)
    assert not result.eligible
    assert any("Agent-name literal" in r for r in result.reasons)


def test_evaluate_pr_contrib_clean_internal_tooling_is_eligible() -> None:
    """End-to-end: contrib#1621-shaped PR (merged by Erik untouched) — clean
    on every gate, no placement reason, no agent literal → eligible."""
    files = [
        {"path": "packages/gptme-runloops/src/gptme_runloops/run_item.py"},
        {"path": "packages/gptme-runloops/tests/test_worker_records_voice_postcall.py"},
    ]
    file_shapes = [
        {
            "path": "packages/gptme-runloops/src/gptme_runloops/run_item.py",
            "status": "modified",
            "patch": "@@ -1,1 +1,2 @@\n+TIMEOUT_S = 30\n",
        },
        {
            "path": "packages/gptme-runloops/tests/test_worker_records_voice_postcall.py",
            "status": "modified",
            "patch": "@@ -1,1 +1,2 @@\n+def test_x(): pass\n",
        },
    ]
    result = _evaluate_contrib(files, file_shapes)
    assert result.eligible, result.reasons
    assert result.category == "internal-tooling"


def test_evaluate_pr_other_repo_contrib_placement_rule_not_applied() -> None:
    """A third repo (not gptme/gptme, not gptme/gptme-contrib) must not go
    through contrib_classify_category — a path shaped exactly like the contrib
    new-top-level-dir trigger must not produce that contrib-specific reason
    text (or fetch contrib-only patch data). gptme/gptme cannot be the control
    here: after #1626 it has its own diff-shape override.
    """
    files = [{"path": "totally_new_top_level_dir/foo.py"}]
    pr_data = _make_clean_pr_data(
        files=files, url="https://github.com/gptme/gptme-cloud/pull/999"
    )
    with (
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(
            self_merge_check, "_fetch_greptile_review_data", return_value=None
        ),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": False, "unresolved": 0, "total": 0},
        ),
        patch.object(self_merge_check, "greptile_summary_score", return_value=None),
        patch.object(
            self_merge_check,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "total": 0, "authors": []},
        ),
        patch.object(
            self_merge_check,
            "fetch_ai_review_status",
            return_value={"accepted": True, "detail": "AI review 5/5 at current head"},
        ),
        patch.object(self_merge_check, "_fetch_contrib_pr_file_shapes") as mock_shapes,
    ):
        result = self_merge_check.evaluate_pr(
            "gptme/gptme-cloud",
            999,
            workspace_repos=["gptme/gptme-cloud"],
        )
    # "Files not in any allowed self-merge category" (plain classify_category)
    # must fire instead of the contrib-specific "New top-level directory"
    # reason — proves the repo-gated dispatch, not a coincidental verdict.
    assert not result.eligible
    assert not any("New top-level directory" in r for r in result.reasons)
    assert any(
        "Files not in any allowed self-merge category" in r for r in result.reasons
    )
    mock_shapes.assert_not_called()
