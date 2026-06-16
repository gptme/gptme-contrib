from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "runs"
    / "autonomous"
    / "session-gate.py"
)
SPEC = importlib.util.spec_from_file_location("session_gate", SCRIPT)
assert SPEC and SPEC.loader
session_gate = importlib.util.module_from_spec(SPEC)
sys.modules["session_gate"] = session_gate
SPEC.loader.exec_module(session_gate)
UTC = timezone.utc


def run_gate(workspace: Path, *args: str) -> int:
    return int(session_gate.main(["--workspace", str(workspace), *args]))


def write_state(workspace: Path, **values: object) -> None:
    state_file = workspace / "state" / "session-gate.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(values) + "\n")


def test_first_run_triggers_and_records_state(tmp_path: Path) -> None:
    exit_code = run_gate(tmp_path)

    assert exit_code == session_gate.RUN
    state = json.loads((tmp_path / "state" / "session-gate.json").read_text())
    assert state["last_decision"] == "run"
    assert "first-run" in state["last_reasons"]


def test_recent_clean_run_skips(tmp_path: Path) -> None:
    write_state(tmp_path, last_allowed_at=datetime.now(UTC).isoformat())

    exit_code = run_gate(tmp_path)

    assert exit_code == session_gate.SKIP
    state = json.loads((tmp_path / "state" / "session-gate.json").read_text())
    assert state["last_decision"] == "skip"


def test_inbox_reply_marker_triggers(tmp_path: Path) -> None:
    write_state(tmp_path, last_allowed_at=datetime.now(UTC).isoformat())
    inbox = tmp_path / "messages" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "handoff.md").write_text("---\nneeds_reply: true\n---\n")

    exit_code = run_gate(tmp_path)

    assert exit_code == session_gate.RUN
    state = json.loads((tmp_path / "state" / "session-gate.json").read_text())
    assert state["last_reasons"] == ["inbox:messages/inbox/handoff.md"]


def test_stale_work_path_triggers(tmp_path: Path) -> None:
    write_state(tmp_path, last_allowed_at=datetime.now(UTC).isoformat())
    queue = tmp_path / "state" / "queue-generated.md"
    queue.write_text("old work\n")
    old = datetime.now(UTC) - timedelta(hours=2)
    os.utime(queue, (old.timestamp(), old.timestamp()))

    exit_code = run_gate(
        tmp_path,
        "--stale-work-paths",
        "state/queue-generated.md",
        "--stale-work-minutes",
        "30",
    )

    assert exit_code == session_gate.RUN
    state = json.loads((tmp_path / "state" / "session-gate.json").read_text())
    assert state["last_reasons"] == ["stale-work:state/queue-generated.md"]


def test_max_interval_triggers(tmp_path: Path) -> None:
    write_state(
        tmp_path,
        last_allowed_at=(datetime.now(UTC) - timedelta(hours=25)).isoformat(),
    )

    exit_code = run_gate(tmp_path, "--max-interval-hours", "24")

    assert exit_code == session_gate.RUN
    state = json.loads((tmp_path / "state" / "session-gate.json").read_text())
    assert state["last_reasons"] == ["max-interval"]


def test_absolute_inbox_path_does_not_crash(tmp_path: Path) -> None:
    # Regression: path.relative_to(workspace) raises ValueError for absolute paths
    # outside the workspace — must fall back to str(path) instead.
    write_state(tmp_path, last_allowed_at=datetime.now(UTC).isoformat())
    external_inbox = tmp_path / "external-inbox"
    external_inbox.mkdir()
    (external_inbox / "msg.md").write_text("---\nneeds_reply: true\n---\n")

    exit_code = run_gate(
        tmp_path,
        "--inbox-paths",
        str(external_inbox),  # absolute path outside workspace subdir
        "--workspace",
        str(
            tmp_path / "workspace-subdir"
        ),  # different workspace → path not relative to it
    )

    assert exit_code == session_gate.RUN
    state = json.loads(
        (tmp_path / "workspace-subdir" / "state" / "session-gate.json").read_text()
    )
    assert any("inbox:" in r for r in state["last_reasons"])


def test_blocked_until_suppresses_first_run(tmp_path: Path) -> None:
    # Regression: blocked_until was checked after first-run was appended to reasons,
    # so `not reasons` was always False and blocked_until had no effect on first-run.
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    write_state(
        tmp_path, blocked_until=future
    )  # no last_allowed_at → first-run normally fires

    exit_code = run_gate(tmp_path)

    assert exit_code == session_gate.SKIP
    state = json.loads((tmp_path / "state" / "session-gate.json").read_text())
    assert state["last_decision"] == "skip"
    assert "blocked-until" in state["last_reasons"]


def test_blocked_until_suppresses_max_interval(tmp_path: Path) -> None:
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    write_state(
        tmp_path,
        blocked_until=future,
        last_allowed_at=(datetime.now(UTC) - timedelta(hours=25)).isoformat(),
    )

    exit_code = run_gate(tmp_path, "--max-interval-hours", "24")

    assert exit_code == session_gate.SKIP
    state = json.loads((tmp_path / "state" / "session-gate.json").read_text())
    assert state["last_decision"] == "skip"
    assert "blocked-until" in state["last_reasons"]
