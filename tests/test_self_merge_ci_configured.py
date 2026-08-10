"""The CI-configuration probe must fail closed when its answer is uncertain.

The gate stops a self-merge when a repository has CI but GitHub reports no
check results, as happened during the 2026-08-06 Actions outage. The probe uses
both active Actions workflows and branch-required status checks, so external CI
is not mistaken for no CI. Only a definitive zero from both sources earns the
no-CI waiver; any API failure remains indeterminate and blocks.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "github" / "self-merge-check.py"
)

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


def _responses(*responses: _Proc):
    pending = list(responses)

    def _run(*args: object, **kwargs: object) -> _Proc:
        assert pending, "unexpected subprocess call"
        return pending.pop(0)

    return _run


@pytest.mark.parametrize(
    ("label", "workflows", "required_checks", "expected"),
    [
        ("active Actions workflow", "2", "0", True),
        ("external required check", "0", "1", True),
        ("both CI sources", "1", "1", True),
        ("no configured CI", "0", "0", False),
        ("workflow probe failed", "", "0", None),
        ("required-check probe failed", "0", "", None),
        ("unparseable workflow count", "null", "0", None),
        ("negative required-check count", "0", "-1", None),
    ],
)
def test_tri_state(
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    workflows: str,
    required_checks: str,
    expected: bool | None,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        _responses(_Proc(0, workflows), _Proc(0, required_checks)),
    )
    assert smc.repo_ci_status("o/r", "main") is expected, label


_NOT_FOUND = _Proc(1, "", "gh: Not Found (HTTP 404)")


@pytest.mark.parametrize("failed_probe", ["workflows", "required_checks"])
def test_api_error_blocks_self_merge(
    monkeypatch: pytest.MonkeyPatch, failed_probe: str
) -> None:
    responses = (
        (_NOT_FOUND, _Proc(0, "0"))
        if failed_probe == "workflows"
        else (_Proc(0, "0"), _NOT_FOUND)
    )
    monkeypatch.setattr(subprocess, "run", _responses(*responses))
    result = _evaluate_with(monkeypatch, smc.repo_ci_status("o/r", "main"))
    assert not result.eligible
    assert any("failing closed" in reason for reason in result.reasons)


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired("gh", 10), OSError("boom")])
def test_exceptions_are_indeterminate(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise exc

    monkeypatch.setattr(subprocess, "run", _raise)
    assert smc.repo_ci_status("o/r", "main") is None


def test_probe_queries_actions_and_required_status_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[list[str]] = []

    def _capture(args: list[str], **kwargs: object) -> _Proc:
        captured.append(list(args))
        return _Proc(0, "0")

    monkeypatch.setattr(subprocess, "run", _capture)
    assert smc.repo_ci_status("o/r", "release/v1") is False

    assert len(captured) == 2
    assert "repos/o/r/actions/workflows" in captured[0]
    assert '[.workflows[]? | select(.state == "active")] | length' in captured[0]
    assert "repos/o/r/branches/release/v1" in captured[1]
    jq_program = captured[1][captured[1].index("--jq") + 1]
    assert "required_status_checks.contexts" in jq_program
    assert "required_status_checks.checks" in jq_program


def _evaluate_with(
    monkeypatch: pytest.MonkeyPatch,
    has_ci: bool | None = None,
    *,
    use_real_ci_probe: bool = False,
) -> Any:
    """Run evaluate_pr against a PR whose statusCheckRollup is empty."""
    pr = {
        "number": 1,
        "title": "t",
        "url": "https://github.com/o/r/pull/1",
        "state": "OPEN",
        "baseRefName": "main",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [],
        "files": [{"path": "tests/test_example.py"}],
        "author": {"login": "TimeToBuildBob"},
        "reviews": [],
        "comments": [],
    }
    monkeypatch.setattr(smc, "fetch_pr", lambda *args, **kwargs: pr)
    if not use_real_ci_probe:
        monkeypatch.setattr(smc, "repo_ci_status", lambda repo, base_ref: has_ci)
    greptile_review = {
        "author": {"login": "greptile-apps[bot]"},
        "submittedAt": "2026-08-09T00:00:00Z",
    }
    monkeypatch.setattr(
        smc,
        "_fetch_greptile_review_data",
        lambda *args, **kwargs: ([greptile_review], []),
    )
    monkeypatch.setattr(smc, "greptile_summary_score", lambda *args, **kwargs: 5)
    monkeypatch.setattr(smc, "get_gh_user", lambda: "TimeToBuildBob")
    monkeypatch.setattr(smc, "merge_permission", lambda repo: True)
    monkeypatch.setattr(smc, "run_gh", lambda *args, **kwargs: "")
    return smc.evaluate_pr("o/r", 1, workspace_repos=["o/r"])


def test_api_error_blocks_via_real_evaluate_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        _responses(_NOT_FOUND, _Proc(0, "0")),
    )
    result = _evaluate_with(monkeypatch, use_real_ci_probe=True)
    assert not result.eligible
    assert any("failing closed" in reason for reason in result.reasons)


def test_indeterminate_ci_blocks_self_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _evaluate_with(monkeypatch, None)
    assert not result.eligible
    assert any("failing closed" in reason for reason in result.reasons)


def test_configured_ci_with_no_checks_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _evaluate_with(monkeypatch, True)
    assert not result.eligible
    assert any("produced no check results" in reason for reason in result.reasons)


def test_genuinely_no_ci_is_waived(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _evaluate_with(monkeypatch, False)
    assert result.eligible
    assert any("No CI configured" in warning for warning in result.warnings)
    assert not any("CI checks not found" in reason for reason in result.reasons)
    assert not any("failing closed" in reason for reason in result.reasons)


class _NoNetwork(RuntimeError):
    pass


def test_evaluate_pr_is_hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explode(*args: object, **kwargs: object) -> None:
        raise _NoNetwork(f"real subprocess invoked: {args!r}")

    monkeypatch.setattr(subprocess, "run", _explode)
    result = _evaluate_with(monkeypatch, False)
    assert any("No CI configured" in warning for warning in result.warnings)
