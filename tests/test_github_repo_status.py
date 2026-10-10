"""Regression tests for scripts/github/repo-status.sh.

Dependabot version updates fire as event=dynamic on the default branch and
would otherwise mask passing product CI (gptme-cloud 2026-09-08: Dependabot
red, Pre-commit green on the same SHA).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "github" / "repo-status.sh"

FAKE_GH = r"""#!/usr/bin/env python3
import json
import os
import sys

argv = sys.argv[1:]

def emit_json(payload):
    print(json.dumps(payload))

if argv[:2] == ["api", "user"]:
    print("TimeToBuildBob")
    raise SystemExit(0)

if len(argv) >= 2 and argv[0] == "api" and argv[1].startswith("repos/"):
    if argv[1].endswith("/commits"):
        print("abc1234")
    else:
        print("master")
    raise SystemExit(0)

if argv[:2] == ["run", "list"]:
    bad = os.environ.get("FAKE_GH_BAD_REPO")
    if bad and f"--repo {bad}" in " ".join(argv):
        mode = os.environ["FAKE_GH_BAD_MODE"]
        if mode == "partial_failure":
            print("[", end="")
            raise SystemExit(1)
        if mode == "null":
            print("null")
        raise SystemExit(0)
    raw = os.environ.get("FAKE_GH_RUNS")
    if raw:
        emit_json(json.loads(raw))
    else:
        emit_json(
            [
                {
                    "conclusion": "success",
                    "status": "completed",
                    "url": "https://example.test/run/1",
                    "name": "Tests",
                    "headSha": "abc1234",
                    "event": "push",
                }
            ]
        )
    raise SystemExit(0)

if argv[:2] == ["workflow", "list"]:
    emit_json([])
    raise SystemExit(0)

raise SystemExit(f"unexpected gh invocation: {argv}")
"""


def _run_script(
    extra_env: dict[str, str] | None = None,
    args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()

        gh = bin_dir / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        env = os.environ.copy()
        env.pop("GH_FORCE_TTY", None)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["GH_USER"] = "TimeToBuildBob"
        env["NO_COLOR"] = "1"
        cache = tmp / "cache"
        cache.mkdir()
        env["XDG_CACHE_HOME"] = str(cache)
        env["BOB_DEFAULT_BRANCH_CACHE_DIR"] = str(cache / "default-branch")
        env["BOB_DISABLED_WORKFLOW_CACHE_DIR"] = str(cache / "disabled-workflows")
        env["BOB_HEAD_SHA_CACHE_DIR"] = str(cache / "head-sha")
        if extra_env:
            env.update(extra_env)

        return subprocess.run(
            ["bash", str(SCRIPT), *(args or ["gptme/gptme-cloud:gptme-cloud"])],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )


def test_passing_product_ci() -> None:
    result = _run_script()
    assert result.returncode == 0, result.stderr
    assert "gptme-cloud: Passing" in result.stdout
    assert "Failing" not in result.stdout


def test_dependabot_failure_does_not_mask_passing_product_ci() -> None:
    runs = [
        {
            "conclusion": "failure",
            "status": "completed",
            "url": "https://example.test/run/dependabot",
            "name": "Dependabot Updates",
            "headSha": "abc1234",
            "event": "dynamic",
        },
        {
            "conclusion": "success",
            "status": "completed",
            "url": "https://example.test/run/precommit",
            "name": "Pre-commit Checks",
            "headSha": "abc1234",
            "event": "push",
        },
    ]
    result = _run_script(extra_env={"FAKE_GH_RUNS": json.dumps(runs)})
    assert result.returncode == 0, result.stderr
    assert "gptme-cloud: Passing" in result.stdout
    assert "Failing" not in result.stdout
    assert "dependabot" not in result.stdout.lower()


def test_dependabot_name_match_without_event_still_skipped() -> None:
    runs = [
        {
            "conclusion": "failure",
            "status": "completed",
            "url": "https://example.test/run/dependabot",
            "name": "Dependabot Alerts",
            "headSha": "abc1234",
            "event": "schedule",
        },
        {
            "conclusion": "success",
            "status": "completed",
            "url": "https://example.test/run/tests",
            "name": "Tests",
            "headSha": "abc1234",
            "event": "schedule",
        },
    ]
    result = _run_script(extra_env={"FAKE_GH_RUNS": json.dumps(runs)})
    assert result.returncode == 0, result.stderr
    assert "gptme-cloud: Passing" in result.stdout
    assert "Failing" not in result.stdout


def test_dependabot_only_still_reports_failing() -> None:
    """If every remaining run is Dependabot, keep it rather than going silent."""
    runs = [
        {
            "conclusion": "failure",
            "status": "completed",
            "url": "https://example.test/run/dependabot",
            "name": "Dependabot Updates",
            "headSha": "abc1234",
            "event": "dynamic",
        }
    ]
    result = _run_script(extra_env={"FAKE_GH_RUNS": json.dumps(runs)})
    assert result.returncode == 0, result.stderr
    assert "gptme-cloud: Failing" in result.stdout
    assert "https://example.test/run/dependabot" in result.stdout


def test_workflow_dispatch_nightly_failure_still_visible() -> None:
    """Nightly full-suite failures are product CI; do not drop workflow_dispatch."""
    runs = [
        {
            "conclusion": "failure",
            "status": "completed",
            "url": "https://example.test/run/nightly",
            "name": "Tests (Full — Nightly)",
            "headSha": "abc1234",
            "event": "workflow_dispatch",
        },
        {
            "conclusion": "success",
            "status": "completed",
            "url": "https://example.test/run/hourly",
            "name": "Tests",
            "headSha": "oldsha00",
            "event": "schedule",
        },
    ]
    result = _run_script(extra_env={"FAKE_GH_RUNS": json.dumps(runs)})
    assert result.returncode == 0, result.stderr
    assert "gptme-cloud: Failing" in result.stdout
    assert "https://example.test/run/nightly" in result.stdout


def test_ghost_startup_failure_only_reports_no_runs_not_unknown() -> None:
    """An all-ghost window (startup_failure, empty name — deleted workflows) must
    report "No runs", not "Unknown ()". Regression for the post-filter empty-array
    fall-through (AI review P1, gptme-contrib#1710)."""
    runs = [
        {
            "conclusion": "startup_failure",
            "status": "completed",
            "url": "https://example.test/run/ghost1",
            "name": "",
            "headSha": "abc1234",
            "event": "push",
        },
        {
            "conclusion": "startup_failure",
            "status": "completed",
            "url": "https://example.test/run/ghost2",
            "name": "",
            "headSha": "abc1234",
            "event": "schedule",
        },
    ]
    result = _run_script(extra_env={"FAKE_GH_RUNS": json.dumps(runs)})
    assert result.returncode == 0, result.stderr
    assert "gptme-cloud: No runs" in result.stdout
    assert "Unknown" not in result.stdout


def test_named_startup_failure_still_surfaces_alongside_ghosts() -> None:
    """A real broken workflow (startup_failure WITH a name) must not be masked by
    ghost runs from deleted workflows sharing the same window."""
    runs = [
        {
            "conclusion": "startup_failure",
            "status": "completed",
            "url": "https://example.test/run/real-broken",
            "name": "Broken Workflow",
            "headSha": "abc1234",
            "event": "push",
        },
        {
            "conclusion": "startup_failure",
            "status": "completed",
            "url": "https://example.test/run/ghost",
            "name": "",
            "headSha": "abc1234",
            "event": "push",
        },
    ]
    result = _run_script(extra_env={"FAKE_GH_RUNS": json.dumps(runs)})
    assert result.returncode == 0, result.stderr
    assert "gptme-cloud: startup_failure" in result.stdout
    assert "No runs" not in result.stdout
    assert "Passing" not in result.stdout


def _assert_bad_response_isolated(mode: str, expected: str) -> None:
    result = _run_script(
        extra_env={"FAKE_GH_BAD_REPO": "gptme/bad", "FAKE_GH_BAD_MODE": mode},
        args=["gptme/bad:bad", "gptme/gptme-cloud:good"],
    )
    out = result.stdout + result.stderr
    assert "integer expression" not in out
    assert "parse error" not in out.lower()
    assert "Cannot iterate" not in out
    assert "Unknown (" not in out
    assert f"bad: {expected}" in out
    assert "good: Passing" in out


def test_empty_success_response_reports_unavailable() -> None:
    _assert_bad_response_isolated("empty", "Unavailable")


def test_null_success_response_reports_unavailable() -> None:
    _assert_bad_response_isolated("null", "Unavailable")


def test_partial_stdout_with_failure_reports_no_actions() -> None:
    _assert_bad_response_isolated("partial_failure", "No Actions")


def test_valid_empty_array_still_reports_no_runs() -> None:
    result = _run_script(extra_env={"FAKE_GH_RUNS": "[]"})
    assert "gptme-cloud: No runs" in result.stdout


def _run(name: str, conclusion: str, sha: str, url: str, event: str = "push") -> dict:
    return {
        "conclusion": conclusion,
        "status": "completed",
        "url": url,
        "name": name,
        "headSha": sha,
        "event": event,
    }


def test_newer_green_workflow_does_not_mask_red_workflow() -> None:
    """A green per-push workflow must not hide an older red cron workflow
    (ErikBjare/bob 2026-10-09: Pre-commit red, later hook-sandbox run green)."""
    runs = [
        _run(
            "Isolated hook sandbox",
            "success",
            "abc1234",
            "https://example.test/run/sandbox",
        ),
        _run(
            "Pre-commit",
            "failure",
            "oldsha00",
            "https://example.test/run/precommit",
            "schedule",
        ),
        _run("Tests", "success", "oldsha00", "https://example.test/run/tests"),
    ]
    result = _run_script(extra_env={"FAKE_GH_RUNS": json.dumps(runs)})
    assert result.returncode == 0, result.stderr
    assert "gptme-cloud: Failing" in result.stdout
    assert "https://example.test/run/precommit" in result.stdout
    assert "stale; HEAD=abc1234, run=oldsha0" in result.stdout


def test_newer_green_run_of_same_workflow_clears_failure() -> None:
    runs = [
        _run(
            "Pre-commit", "success", "abc1234", "https://example.test/run/precommit-2"
        ),
        _run(
            "Pre-commit", "failure", "oldsha00", "https://example.test/run/precommit-1"
        ),
    ]
    result = _run_script(extra_env={"FAKE_GH_RUNS": json.dumps(runs)})
    assert result.returncode == 0, result.stderr
    assert "gptme-cloud: Passing" in result.stdout
    assert "Failing" not in result.stdout


def _pending(name: str, sha: str, status: str = "queued") -> dict:
    return {
        "conclusion": "",
        "status": status,
        "url": f"https://example.test/run/{name}-{sha}",
        "name": name,
        "headSha": sha,
        "event": "push",
    }


def test_several_queued_runs_fall_back_to_newest_completed() -> None:
    """A backed-up runner queue leaves more than one run pending; the status
    must come from the newest completed run, not from .[1] (aw-server-rust
    2026-10-10 printed "In progress (no previous run)")."""
    runs = [
        _pending("Lint", "abc1234"),
        _pending("Build", "abc1234"),
        _pending("Build", "oldsha00", status="in_progress"),
        _run("Build", "success", "oldsha00", "https://example.test/run/build-old"),
        _run("Lint", "success", "oldsha00", "https://example.test/run/lint-old"),
    ]
    result = _run_script(extra_env={"FAKE_GH_RUNS": json.dumps(runs)})
    assert result.returncode == 0, result.stderr
    assert "gptme-cloud: Passing (run in progress)" in result.stdout
    assert "no previous run" not in result.stdout


def test_only_pending_runs_still_report_no_previous_run() -> None:
    runs = [_pending("Lint", "abc1234"), _pending("Build", "abc1234")]
    result = _run_script(extra_env={"FAKE_GH_RUNS": json.dumps(runs)})
    assert result.returncode == 0, result.stderr
    assert "gptme-cloud: In progress (no previous run)" in result.stdout
