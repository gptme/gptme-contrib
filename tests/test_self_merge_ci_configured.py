"""`repo_has_ci_configured` must fail closed when it cannot determine the answer.

This guard exists to stop a self-merge when a repo *has* CI but the checks
produced no result — the shape seen during the 2026-08-06 GitHub Actions
outage, where `statusCheckRollup` came back empty on repos that very much do
run CI.

The subtle failure mode, caught by our own self-hosted AI reviewer on
gptme/gptme-contrib#1382 (see ErikBjare/bob#1122): the helper originally
returned a plain `bool` and collapsed *every* failure — timeout, 5xx, rate
limit, auth — into `False`, i.e. "this repo has no CI". During an outage the
workflows lookup fails for the same reason the checks are missing, so the
guard said "no CI configured", the PR stayed eligible, and the outage merge
this check exists to block went through anyway.

So the helper is tri-state and the call site may only waive the CI requirement
on a definitive `False`.
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

# Register in sys.modules before exec_module — the script defines @dataclass at
# import time, and dataclasses resolves annotations via sys.modules[__module__].
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


@pytest.mark.parametrize(
    ("label", "proc", "expected"),
    [
        ("two workflow files", _Proc(0, "2"), True),
        ("one workflow file", _Proc(0, "1"), True),
        ("directory exists but is empty", _Proc(0, "0"), False),
        (
            "404 — genuinely no workflows dir",
            _Proc(1, "", "gh: Not Found (HTTP 404)"),
            False,
        ),
        # Everything below is indeterminate and MUST be None, not False.
        ("rate limited", _Proc(1, "", "API rate limit exceeded"), None),
        ("server error", _Proc(1, "", "HTTP 502 Bad Gateway"), None),
        ("auth failure", _Proc(1, "", "HTTP 401 Bad credentials"), None),
        ("empty stdout on success", _Proc(0, ""), None),
        ("unparseable stdout", _Proc(0, "null"), None),
    ],
)
def test_tri_state(
    monkeypatch: pytest.MonkeyPatch, label: str, proc: _Proc, expected: bool | None
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: proc)
    assert smc.repo_has_ci_configured("o/r") is expected, label


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired("gh", 10), OSError("boom")])
def test_exceptions_are_indeterminate(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """A timeout is the single likeliest symptom of the outage we guard against."""

    def _raise(*a: object, **k: object) -> None:
        raise exc

    monkeypatch.setattr(subprocess, "run", _raise)
    assert smc.repo_has_ci_configured("o/r") is None


def _evaluate_with(monkeypatch: pytest.MonkeyPatch, has_ci: bool | None) -> Any:
    """Run evaluate_pr against a PR whose statusCheckRollup came back empty."""
    pr = {
        "number": 1,
        "title": "t",
        "url": "https://github.com/o/r/pull/1",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [],
        "files": [],
        "author": {"login": "TimeToBuildBob"},
        "reviews": [],
        "comments": [],
    }
    monkeypatch.setattr(smc, "fetch_pr", lambda *a, **k: pr)
    monkeypatch.setattr(smc, "repo_has_ci_configured", lambda repo: has_ci)
    monkeypatch.setattr(smc, "_fetch_greptile_review_data", lambda *a, **k: ([], []))
    monkeypatch.setattr(smc, "get_gh_user", lambda: "TimeToBuildBob")
    return smc.evaluate_pr("o/r", 1, workspace_repos=None)


def test_indeterminate_ci_blocks_self_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression: None must disqualify, not fall through to a warning.

    Without this, an API error during an outage re-enables the exact merge the
    guard was added to prevent.
    """
    result = _evaluate_with(monkeypatch, None)
    assert not result.eligible
    assert any("failing closed" in r for r in result.reasons)


def test_configured_ci_with_no_checks_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _evaluate_with(monkeypatch, True)
    assert not result.eligible
    assert any("produced no check results" in r for r in result.reasons)


def test_genuinely_no_ci_is_waived(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repo that really has no workflows still gets the documented waiver."""
    result = _evaluate_with(monkeypatch, False)
    assert any("No CI configured" in w for w in result.warnings)
    # The CI requirement is waived; other gates (e.g. review) may still block.
    assert not any("CI checks not found" in r for r in result.reasons)
    assert not any("failing closed" in r for r in result.reasons)
