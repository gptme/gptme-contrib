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
from collections.abc import Callable
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
        # A bare 404 on *every* endpoint — including the repo-readability probe
        # — means we could not read the repo at all, so it is indeterminate.
        # The "readable repo, no workflows dir" case needs two distinct gh
        # responses and lives in test_contents_404_is_disambiguated_by_repo_probe.
        (
            "404 on contents and on the repo probe",
            _Proc(1, "", "gh: Not Found (HTTP 404)"),
            None,
        ),
        # Everything below is indeterminate and MUST be None, not False.
        ("rate limited", _Proc(1, "", "API rate limit exceeded"), None),
        ("server error", _Proc(1, "", "HTTP 502 Bad Gateway"), None),
        ("auth failure", _Proc(1, "", "HTTP 401 Bad credentials"), None),
        ("empty stdout on success", _Proc(0, ""), None),
        ("unparseable stdout", _Proc(0, "null"), None),
        # The jq type-guard sentinel: the contents path resolved to something
        # that is not a directory listing, so there is nothing to count.
        ("non-array payload (type-guard sentinel)", _Proc(0, "-1"), None),
    ],
)
def test_tri_state(
    monkeypatch: pytest.MonkeyPatch, label: str, proc: _Proc, expected: bool | None
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: proc)
    assert smc.repo_has_ci_configured("o/r") is expected, label


def _route_gh(contents: _Proc, repo_probe: _Proc) -> Callable[..., _Proc]:
    """Fake ``subprocess.run`` that answers per gh endpoint.

    ``repo_has_ci_configured`` may now make a second call — the repo-readability
    probe — so a single canned response is no longer enough to model it.
    """

    def _run(args: list[str], **kwargs: object) -> _Proc:
        url = args[2] if len(args) > 2 else ""
        return contents if url.endswith("/contents/.github/workflows") else repo_probe

    return _run


_NOT_FOUND = _Proc(1, "", "gh: Not Found (HTTP 404)")
_REPO_OK = _Proc(0, "o/r")


@pytest.mark.parametrize(
    ("label", "repo_probe", "expected"),
    [
        # The repo is readable, so the 404 really was about the missing path.
        ("path-level 404, repo readable", _REPO_OK, False),
        # The repo itself 404s: we cannot read it at all, so we learned nothing
        # about its CI. Waiving here is the fail-open this guard exists to stop.
        ("repo-level 404 (unreadable/renamed/typo'd slug)", _NOT_FOUND, None),
        # A token with `Pull requests: read` but no `Contents: read` sees a 404
        # on both endpoints — indistinguishable from the above, and equally
        # must not waive.
        ("token cannot read contents", _Proc(1, "", "gh: Not Found (HTTP 404)"), None),
        # The probe itself is unreliable — still not a licence to waive.
        ("repo probe rate limited", _Proc(1, "", "API rate limit exceeded"), None),
        ("repo probe 5xx", _Proc(1, "", "HTTP 502 Bad Gateway"), None),
        ("repo probe returns nothing", _Proc(0, ""), None),
    ],
)
def test_contents_404_is_disambiguated_by_repo_probe(
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    repo_probe: _Proc,
    expected: bool | None,
) -> None:
    """A 404 on the contents endpoint must not by itself mean "no CI".

    Regression for the P1 on gptme/gptme-contrib#1382: ``gh`` prints exactly
    ``gh: Not Found (HTTP 404)`` both for a repo that does not exist and for a
    real repo missing that path, so substring-matching stderr classified an
    unreadable repo as "definitively no CI" and waived the CI gate — the same
    fail-open, moved from the checks lookup to the workflows lookup.
    """
    monkeypatch.setattr(subprocess, "run", _route_gh(_NOT_FOUND, repo_probe))
    assert smc.repo_has_ci_configured("o/r") is expected, label


def test_repo_probe_not_called_when_contents_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extra probe is only paid on the 404 path, not on every lookup."""
    calls: list[str] = []

    def _run(args: list[str], **kwargs: object) -> _Proc:
        calls.append(args[2])
        return _Proc(0, "2")

    monkeypatch.setattr(subprocess, "run", _run)
    assert smc.repo_has_ci_configured("o/r") is True
    assert calls == ["repos/o/r/contents/.github/workflows"], calls


def test_repo_level_404_blocks_self_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: an unreadable repo must disqualify, not warn-and-pass."""
    monkeypatch.setattr(subprocess, "run", _route_gh(_NOT_FOUND, _NOT_FOUND))
    result = _evaluate_with(monkeypatch, smc.repo_has_ci_configured("o/r"))
    assert not result.eligible
    assert any("failing closed" in r for r in result.reasons)


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired("gh", 10), OSError("boom")])
def test_exceptions_are_indeterminate(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """A timeout is the single likeliest symptom of the outage we guard against."""

    def _raise(*a: object, **k: object) -> None:
        raise exc

    monkeypatch.setattr(subprocess, "run", _raise)
    assert smc.repo_has_ci_configured("o/r") is None


def test_workflows_lookup_jq_is_type_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The jq program must only count when the payload is a directory listing.

    The Contents API returns a JSON *array* for a directory but a JSON *object*
    for a regular file. A bare ``--jq length`` counts an object's keys instead,
    so a repo where ``.github/workflows`` happened to be a file would report
    ``True`` ("CI is configured") rather than the documented indeterminate
    ``None``. Type-guard in jq and emit a negative sentinel for non-arrays.
    """
    captured: list[list[str]] = []

    def _capture(args: list[str], **kwargs: object) -> _Proc:
        captured.append(list(args))
        return _Proc(0, "2")

    monkeypatch.setattr(subprocess, "run", _capture)
    assert smc.repo_has_ci_configured("o/r") is True

    assert captured, "gh was never invoked"
    argv = captured[0]
    assert "--jq" in argv, f"no --jq in argv: {argv!r}"
    jq_program = argv[argv.index("--jq") + 1]
    assert "array" in jq_program, f"jq program is not type-guarded: {jq_program!r}"
    assert "-1" in jq_program, f"jq program has no non-array sentinel: {jq_program!r}"


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
    # evaluate_pr reaches two more helpers that shell out to a real `gh`:
    #   * merge_permission — and it is @cache'd, so an unstubbed call poisons the
    #     cache for every later test in the session with a live network result.
    #     A definitive False would append a disqualifying reason and mask the
    #     CI-gate reasons these tests assert on.
    #   * run_gh — reached via fetch_greptile_status's summary-comment fallback,
    #     which fires because the stubbed review data is empty.
    # Stub both so the CI gate is the only thing under test.
    monkeypatch.setattr(smc, "merge_permission", lambda repo: True)
    monkeypatch.setattr(smc, "run_gh", lambda *a, **k: "")
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


class _NoNetwork(RuntimeError):
    """Raised if a test reaches a real subprocess. Not caught by the module.

    Deliberately not a subclass of anything ``self-merge-check.py`` catches
    (``TimeoutExpired`` / ``OSError`` / ``JSONDecodeError``), so an escape shows
    up as an error rather than being silently swallowed into an indeterminate
    result that happens to match the assertion.
    """


def test_evaluate_pr_is_hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_evaluate_with`` must not shell out to a real ``gh``.

    ``evaluate_pr`` reaches ``merge_permission`` and the ``fetch_greptile_status``
    summary-comment fallback, neither of which was stubbed. Both ran a real
    ``gh api`` against github.com, so these tests were network-dependent: they
    could hang on a slow API, flip on rate limits, or — worse — quietly change
    the assertion surface (a definitive "no merge permission" adds a
    disqualifying reason, which would mask exactly the CI-gate reasons under
    test).
    """

    def _explode(*a: object, **k: object) -> None:
        raise _NoNetwork(f"real subprocess invoked: {a!r}")

    monkeypatch.setattr(subprocess, "run", _explode)
    result = _evaluate_with(monkeypatch, False)
    assert any("No CI configured" in w for w in result.warnings)
