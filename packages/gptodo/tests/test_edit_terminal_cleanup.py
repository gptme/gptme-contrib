"""Tests for stripping stale actionable/blocker metadata on terminal transitions.

When `gptodo edit --set state done|cancelled` lands a task in a terminal state,
the now-stale `next_action`, `waiting_for`, `waiting_since`, and `wait` fields
should be removed (TASKS.md best-practice #7). `tracking_issue` and
`upstream_coordination_id` are preserved for permanent traceability, and
recurring tasks (which reset to todo) keep their fields.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.utils import load_tasks

ACTIVE_WITH_NEXT_ACTION = """\
---
state: active
created: 2026-06-01T00:00:00+00:00
next_action: Run the thing
tracking_issue: https://github.com/org/repo/issues/1
---
# Active Task
"""

WAITING_TASK = """\
---
state: waiting
created: 2026-06-01T00:00:00+00:00
waiting_for: some-dependency
waiting_since: 2026-06-01
wait: 2026-06-20T00:00:00+00:00
---
# Waiting Task
"""

RECURRING_TASK = """\
---
state: active
created: 2026-06-01T00:00:00+00:00
next_action: Do the recurring thing
recur: 7d
---
# Recurring Task
"""


def _meta(tasks_dir: Path, task_id: str) -> dict:
    return next(t for t in load_tasks(tasks_dir) if t.id == task_id).metadata


def test_done_strips_next_action_keeps_tracking_issue(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(ACTIVE_WITH_NEXT_ACTION)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["edit", "my-task", "--set", "state", "done"])
    assert result.exit_code == 0, result.output

    meta = _meta(tasks_dir, "my-task")
    assert meta["state"] == "done"
    assert "next_action" not in meta
    # Traceability fields are preserved.
    assert meta["tracking_issue"] == "https://github.com/org/repo/issues/1"


def test_cancelled_strips_waiting_fields(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "blocked.md").write_text(WAITING_TASK)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["edit", "blocked", "--set", "state", "cancelled"])
    assert result.exit_code == 0, result.output

    meta = _meta(tasks_dir, "blocked")
    assert meta["state"] == "cancelled"
    for field in ("waiting_for", "waiting_since", "wait"):
        assert field not in meta


def test_recurring_cancelled_strips_fields(tmp_path: Path, monkeypatch) -> None:
    """Recurring cancelled is terminal — stale fields must be stripped."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "recurring.md").write_text(RECURRING_TASK)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["edit", "recurring", "--set", "state", "cancelled"])
    assert result.exit_code == 0, result.output

    meta = _meta(tasks_dir, "recurring")
    assert meta["state"] == "cancelled"
    assert "next_action" not in meta


def test_recurring_done_keeps_fields(tmp_path: Path, monkeypatch) -> None:
    """Recurring tasks reset to todo and must retain next_action."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "recurring.md").write_text(RECURRING_TASK)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["edit", "recurring", "--set", "state", "done"])
    assert result.exit_code == 0, result.output

    meta = _meta(tasks_dir, "recurring")
    # Recur logic resets to todo and keeps next_action.
    assert meta["state"] == "todo"
    assert meta["next_action"] == "Do the recurring thing"
