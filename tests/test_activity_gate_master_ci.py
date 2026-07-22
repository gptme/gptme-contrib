"""Regression tests for master-CI event filtering in activity-gate.sh."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "github" / "activity-gate.sh"

FAKE_GH = r"""#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys

argv = sys.argv[1:]
if argv[:2] == ["run", "list"]:
    runs = json.loads(os.environ.get("TEST_MASTER_RUNS", "[]"))
    if "--event" in argv:
        event = argv[argv.index("--event") + 1]
        runs = [run for run in runs if run.get("event") == event]
    limit = int(argv[argv.index("--limit") + 1])
    print(json.dumps(runs[:limit]))
    raise SystemExit(0)
if argv[:2] in (["pr", "list"], ["issue", "list"]):
    print("[]")
    raise SystemExit(0)
if argv and argv[0] == "api":
    raise SystemExit(0)
raise SystemExit(0)
"""


def _run_gate(tmp_path: Path, runs: list[dict]) -> subprocess.CompletedProcess[str]:
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["TEST_MASTER_RUNS"] = json.dumps(runs)
    env["GH_CACHE_TTL_RUN"] = "0"
    return subprocess.run(
        [
            str(SCRIPT),
            "--author",
            "test-author",
            "--repo",
            "owner/repo",
            "--state-dir",
            str(tmp_path / "state"),
            "--format",
            "jsonl",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _failed_run(run_id: int, event: str) -> dict:
    return {
        "databaseId": run_id,
        "name": f"workflow-{event}",
        "conclusion": "failure",
        "createdAt": "2026-07-22T13:59:34Z",
        "event": event,
    }


def test_detached_events_are_not_reported_as_master_ci(tmp_path: Path) -> None:
    """Detached/manual events associated with master are not regressions."""
    runs = [
        _failed_run(101, "dynamic"),
        _failed_run(102, "workflow_dispatch"),
        _failed_run(103, "repository_dispatch"),
    ]
    result = _run_gate(tmp_path, runs)

    assert result.returncode == 1, result.stderr
    assert result.stdout == ""


def test_branch_health_events_are_reported_as_master_ci(tmp_path: Path) -> None:
    runs = [
        _failed_run(104, "push"),
        _failed_run(105, "schedule"),
        _failed_run(106, "workflow_call"),
    ]
    result = _run_gate(tmp_path, runs)

    assert result.returncode == 0, result.stderr
    items = [json.loads(line) for line in result.stdout.splitlines()]
    assert [(item["type"], item["number"]) for item in items] == [
        ("master_ci_failure", 104),
        ("master_ci_failure", 105),
        ("master_ci_failure", 106),
    ]


def test_nonpush_window_does_not_hide_older_push_failure(tmp_path: Path) -> None:
    """The API event filter runs before the three-run result limit."""
    runs = [
        _failed_run(107, "schedule"),
        _failed_run(108, "workflow_dispatch"),
        _failed_run(109, "dynamic"),
        _failed_run(110, "push"),
    ]

    result = _run_gate(tmp_path, runs)

    assert result.returncode == 0, result.stderr
    items = [json.loads(line) for line in result.stdout.splitlines()]
    assert ("master_ci_failure", 110) in [
        (item["type"], item["number"]) for item in items
    ]
