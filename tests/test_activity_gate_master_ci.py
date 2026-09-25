"""Regression tests for master-CI event filtering in activity-gate.sh."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import datetime, timedelta, timezone
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
    if "--status" in argv:
        status = argv[argv.index("--status") + 1]
        runs = [run for run in runs if run.get("conclusion") == status]
    if "--created" in argv:
        # Mirror the real gh contract: plain ISO date or '>=<date>'. Anything
        # else is malformed and must fail loudly so a regression in the
        # --created argument is caught instead of silently ignored.
        created = argv[argv.index("--created") + 1]
        if created.startswith(">="):
            since = created[2:]
            runs = [run for run in runs if run.get("createdAt", "") >= since]
        else:
            since = created
        if len(since) != 10 or not since[:4].isdigit() or since[4] != "-" or not since[5:7].isdigit() or since[7] != "-" or not since[8:].isdigit():
            print(f"error: invalid --created value: {created!r}", file=sys.stderr)
            raise SystemExit(2)
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


def _run(
    run_id: int,
    event: str,
    *,
    conclusion: str = "failure",
    name: str | None = None,
    # Default to inside the gate's 3-day --created window so the fake gh's
    # (real-contract) window filter does not drop fixtures as they age.
    created_at: str | None = None,
) -> dict:
    if created_at is None:
        created_at = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    return {
        "databaseId": run_id,
        "name": name or f"workflow-{event}",
        "conclusion": conclusion,
        "createdAt": created_at,
        "event": event,
    }


def _ts(seconds_ago: int, *, fractional: bool = False) -> str:
    """Fixture timestamp relative to now, so tests stay inside the gate's
    3-day --created window regardless of when they run (hermetic suite)."""
    t = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).replace(
        microsecond=0
    )
    base = t.strftime("%Y-%m-%dT%H:%M:%S")
    return base + (".000Z" if fractional else "Z")


def _failed_run(run_id: int, event: str) -> dict:
    return _run(run_id, event)


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
    """The API event filter runs before the result limit."""
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


def test_newer_success_of_same_workflow_suppresses_stale_failure(
    tmp_path: Path,
) -> None:
    """A recovered Tests run must not re-dispatch 11-day-old failures."""
    runs = [
        _run(
            201,
            "schedule",
            conclusion="failure",
            name="Tests",
            created_at=_ts(2 * 86400),
        ),
        _run(
            202,
            "schedule",
            conclusion="success",
            name="Tests",
            created_at=_ts(3600),
        ),
    ]
    result = _run_gate(tmp_path, runs)

    assert result.returncode == 1, result.stderr
    assert result.stdout == ""


def test_manual_success_does_not_suppress_push_failure(tmp_path: Path) -> None:
    """Suppression is event-consistent.

    Manual/dispatch runs are excluded as failures because they are unrelated
    to default-branch health; the same reasoning must apply to successes, or
    a workflow_dispatch success would silently clear a real push failure.
    """
    runs = [
        _run(
            601,
            "push",
            conclusion="failure",
            name="Tests",
            created_at=_ts(7200),
        ),
        _run(
            602,
            "workflow_dispatch",
            conclusion="success",
            name="Tests",
            created_at=_ts(3600),
        ),
    ]
    result = _run_gate(tmp_path, runs)

    assert result.returncode == 0, result.stderr
    items = [json.loads(line) for line in result.stdout.splitlines()]
    assert ("master_ci_failure", 601) in [
        (item["type"], item["number"]) for item in items
    ]


def test_nightly_failure_is_not_push_ci_master_failure(tmp_path: Path) -> None:
    """Nightly full-suite red must not occupy the push-CI master_ci slot."""
    runs = [
        _run(
            301,
            "schedule",
            conclusion="failure",
            name="Tests (Full — Nightly)",
            created_at=_ts(2 * 86400 + 2406),
        ),
        _run(
            302,
            "schedule",
            conclusion="success",
            name="Tests",
            created_at=_ts(2 * 86400),
        ),
    ]
    result = _run_gate(tmp_path, runs)

    assert result.returncode == 1, result.stderr
    assert result.stdout == ""


def test_earlier_success_in_same_second_does_not_suppress(tmp_path: Path) -> None:
    """Timestamps must be compared chronologically, not lexicographically.

    The success here is *earlier* (``:29Z`` = 29.000) than the failure
    (``:29.500Z``), but raw string comparison puts it *after* the failure
    because ``Z`` (0x5A) > ``.`` (0x2E). Without normalization this failure
    would be wrongly suppressed; with it, the success is not newer and the
    failure is emitted.
    """
    instant = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0)
    runs = [
        _run(
            501,
            "schedule",
            conclusion="failure",
            name="Tests",
            created_at=instant.strftime("%Y-%m-%dT%H:%M:%S") + ".500Z",
        ),
        _run(
            502,
            "schedule",
            conclusion="success",
            name="Tests",
            created_at=instant.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    ]
    result = _run_gate(tmp_path, runs)

    assert result.returncode == 0, result.stderr
    items = [json.loads(line) for line in result.stdout.splitlines()]
    assert ("master_ci_failure", 501) in [
        (item["type"], item["number"]) for item in items
    ]


def test_same_second_success_is_not_strictly_newer(tmp_path: Path) -> None:
    """Normalization collapses fractional seconds, so equal is not "newer".

    A same-second success must not clear a failure: the sub-second order is
    unrecoverable after normalization and the gate fails closed.
    """
    instant = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0)
    runs = [
        _run(
            503,
            "schedule",
            conclusion="failure",
            name="Tests",
            created_at=instant.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
        _run(
            504,
            "schedule",
            conclusion="success",
            name="Tests",
            created_at=instant.strftime("%Y-%m-%dT%H:%M:%S") + ".000Z",
        ),
    ]
    result = _run_gate(tmp_path, runs)

    assert result.returncode == 0, result.stderr
    items = [json.loads(line) for line in result.stdout.splitlines()]
    assert ("master_ci_failure", 503) in [
        (item["type"], item["number"]) for item in items
    ]


def test_unrecovered_tests_failure_is_still_emitted(tmp_path: Path) -> None:
    runs = [
        _run(
            401,
            "schedule",
            conclusion="failure",
            name="Tests",
            created_at=_ts(2 * 86400),
        ),
        _run(
            402,
            "schedule",
            conclusion="success",
            name="Pre-commit",
            created_at=_ts(3600),
        ),
    ]
    result = _run_gate(tmp_path, runs)

    assert result.returncode == 0, result.stderr
    items = [json.loads(line) for line in result.stdout.splitlines()]
    assert [(item["type"], item["number"]) for item in items] == [
        ("master_ci_failure", 401),
    ]
