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
            ["bash", str(SCRIPT), "gptme/gptme-cloud:gptme-cloud"],
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
