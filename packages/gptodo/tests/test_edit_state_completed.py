"""Tests for auto-injection of completed when editing state to done/cancelled.

When `gptodo edit --set state done` (or cancelled) is called, `completed` should
be automatically populated with the current UTC datetime if it is not already set.
This provides a machine-readable completion timestamp without requiring callers to
set it manually.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
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

RECURRING_TASK_WITH_COMPLETED = """\
---
state: active
created: 2026-06-16T00:00:00+00:00
recur: 7d
wait: 2026-06-23T00:00:00+00:00
completed: 2026-06-10T12:00:00+00:00
---
# A Recurring Task With A Stale Stamp
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


def test_reopening_terminal_task_clears_completed_for_next_cycle(
    tmp_path: Path, monkeypatch
) -> None:
    """Reopening a terminal task drops the stamp so re-completion re-stamps it.

    Without the reopen-clear, the stale first-close value survives and the
    ``not post.metadata.get("completed")`` guard suppresses the second stamp,
    leaving the task permanently advertising its *first* completion time.
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(
        ACTIVE_TASK_WITH_COMPLETED.replace("state: active", "state: done")
    )

    monkeypatch.chdir(tmp_path)
    reopened = CliRunner().invoke(cli, ["edit", "my-task", "--set", "state", "active", "--force"])
    assert reopened.exit_code == 0, reopened.output
    assert "completed" not in load_tasks(tasks_dir)[0].metadata

    closed = CliRunner().invoke(cli, ["edit", "my-task", "--set", "state", "done"])
    assert closed.exit_code == 0, closed.output
    completed = datetime.fromisoformat(str(load_tasks(tasks_dir)[0].metadata["completed"]))
    assert completed > datetime(2026, 6, 10, 12, tzinfo=timezone.utc)


def test_reopening_preserves_explicitly_set_completed(tmp_path: Path, monkeypatch) -> None:
    """An explicit ``--set completed`` survives the reopen-clear in the same edit."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(
        ACTIVE_TASK_WITH_COMPLETED.replace("state: active", "state: done")
    )

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "edit",
            "my-task",
            "--set",
            "state",
            "active",
            "--set",
            "completed",
            "2026-01-01T00:00:00+00:00",
            "--force",
        ],
    )

    assert result.exit_code == 0, result.output
    assert load_tasks(tasks_dir)[0].metadata["completed"] == "2026-01-01T00:00:00+00:00"


@pytest.mark.parametrize(
    "recur_value",
    [
        "sometimes",  # malformed — parser rejects
        "0 9 * * 1",  # cron — documented-valid but parse_recur_interval() returns None
    ],
)
def test_unparseable_recur_is_terminal_and_gets_completed(
    tmp_path: Path, monkeypatch, recur_value: str
) -> None:
    """recur: values the parser rejects are terminal, so they get stamped.

    The recurrence handler resets a task to todo only when
    ``parse_recur_interval()`` returns an interval; everything else (malformed
    strings *and* cron expressions, which are documented-valid but not yet
    computed) is left sitting in done. The terminal predicate must agree, or
    such tasks end up permanently done with no completed stamp.
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(
        RECURRING_TASK.replace("recur: 7d", f"recur: {recur_value!r}")
    )

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["edit", "my-task", "--set", "state", "done"])

    assert result.exit_code == 0, result.output
    task = load_tasks(tasks_dir)[0]
    assert task.metadata["state"] == "done", "unparseable recur must not reset to todo"
    assert "completed" in task.metadata
    assert "wait" not in task.metadata, "terminal tasks must not keep a stale wait:"


def test_explicit_completed_clear_wins_over_auto_stamp(tmp_path: Path, monkeypatch) -> None:
    """``--set completed none`` is not silently undone by the auto-stamp."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(ACTIVE_TASK_WITH_COMPLETED)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "edit",
            "my-task",
            "--set",
            "state",
            "done",
            "--set",
            "completed",
            "none",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "completed" not in load_tasks(tasks_dir)[0].metadata


def test_recurring_reset_removes_existing_completed(tmp_path: Path, monkeypatch) -> None:
    """The recurrence reset drops a stale stamp carried in from a previous cycle."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(RECURRING_TASK_WITH_COMPLETED)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["edit", "my-task", "--set", "state", "done"])

    assert result.exit_code == 0, result.output
    task = load_tasks(tasks_dir)[0]
    assert task.metadata["state"] == "todo"
    assert "completed" not in task.metadata
