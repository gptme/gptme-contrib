"""Tests for auto-injection of completed when editing state to done/cancelled.

When `gptodo edit --set state done` (or cancelled) is called, `completed` should
be automatically populated with the current UTC datetime if it is not already set.
This provides a machine-readable completion timestamp without requiring callers to
set it manually.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.utils import load_tasks


ACTIVE_TASK = """\
---
state: active
created: 2026-06-16T00:00:00+00:00
---
# Some Active Task
"""

ACTIVE_TASK_WITH_COMPLETED = """\
---
state: active
created: 2026-06-16T00:00:00+00:00
completed: 2026-06-10T12:00:00+00:00
---
# Task With Pre-existing Completed
"""

RECURRING_TASK = """\
---
state: active
created: 2026-06-16T00:00:00+00:00
recur: 7d
wait: 2026-06-23T00:00:00+00:00
---
# A Recurring Task
"""


def test_edit_state_done_auto_sets_completed(tmp_path: Path, monkeypatch) -> None:
    """Transitioning to done auto-injects completed as a UTC ISO datetime."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(ACTIVE_TASK)

    monkeypatch.chdir(tmp_path)
    before = datetime.now(timezone.utc).replace(microsecond=0)
    result = CliRunner().invoke(cli, ["edit", "my-task", "--set", "state", "done"])
    after = datetime.now(timezone.utc)

    assert result.exit_code == 0, f"edit failed: {result.output}"

    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.metadata["state"] == "done"
    assert "completed" in task.metadata, "completed should be auto-set on done transition"
    injected = datetime.fromisoformat(str(task.metadata["completed"]))
    if injected.tzinfo is None:
        injected = injected.replace(tzinfo=timezone.utc)
    assert (
        before <= injected <= after
    ), f"completed {injected!r} not between {before!r} and {after!r}"


def test_edit_state_cancelled_auto_sets_completed(tmp_path: Path, monkeypatch) -> None:
    """Transitioning to cancelled auto-injects completed as a UTC ISO datetime."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(ACTIVE_TASK)

    monkeypatch.chdir(tmp_path)
    before = datetime.now(timezone.utc).replace(microsecond=0)
    result = CliRunner().invoke(cli, ["edit", "my-task", "--set", "state", "cancelled"])
    after = datetime.now(timezone.utc)

    assert result.exit_code == 0, f"edit failed: {result.output}"

    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.metadata["state"] == "cancelled"
    assert "completed" in task.metadata, "completed should be auto-set on cancelled transition"
    injected = datetime.fromisoformat(str(task.metadata["completed"]))
    if injected.tzinfo is None:
        injected = injected.replace(tzinfo=timezone.utc)
    assert before <= injected <= after


def test_edit_state_done_does_not_override_existing_completed(tmp_path: Path, monkeypatch) -> None:
    """If completed is already set, it should not be overwritten on done transition."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(ACTIVE_TASK_WITH_COMPLETED)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["edit", "my-task", "--set", "state", "done"])

    assert result.exit_code == 0, f"edit failed: {result.output}"

    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    # YAML parses ISO datetimes to Python datetime objects; compare via fromisoformat
    stored = datetime.fromisoformat(str(tasks[0].metadata["completed"]))
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    assert stored == datetime(
        2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc
    ), "pre-existing completed must not change"


def test_edit_unrelated_field_does_not_inject_completed(tmp_path: Path, monkeypatch) -> None:
    """Editing an unrelated field on an active task must NOT inject completed."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(ACTIVE_TASK)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["edit", "my-task", "--set", "priority", "high"])

    assert result.exit_code == 0, f"edit failed: {result.output}"

    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    assert (
        "completed" not in tasks[0].metadata
    ), "completed must not be injected when only editing an unrelated field"


def test_edit_state_done_recurring_does_not_set_completed(tmp_path: Path, monkeypatch) -> None:
    """Recurring tasks transitioning to done reset to todo and must not get completed."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(RECURRING_TASK)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["edit", "my-task", "--set", "state", "done"])

    assert result.exit_code == 0, f"edit failed: {result.output}"

    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    task = tasks[0]
    # Recurring tasks reset to todo — completed should not be stamped on reset
    assert task.metadata["state"] == "todo", "recurring task must reset to todo"
    assert (
        "completed" not in task.metadata
    ), "recurring done tasks must not carry a completed stamp into the next cycle"
