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
from gptodo.validate_frontmatter import validate_timestamp_syntax


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
    assert "completed: None ->" in result.output
    injected = datetime.fromisoformat(str(task.metadata["completed"]))
    if injected.tzinfo is None:
        injected = injected.replace(tzinfo=timezone.utc)
    assert before <= injected <= after, (
        f"completed {injected!r} not between {before!r} and {after!r}"
    )
    raw_frontmatter = (tasks_dir / "my-task.md").read_text().split("---", maxsplit=2)[1]
    assert validate_timestamp_syntax(raw_frontmatter) == []


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


def test_edit_state_done_replaces_stale_completed(tmp_path: Path, monkeypatch) -> None:
    """A task reopened outside gptodo gets a fresh completion timestamp."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(ACTIVE_TASK_WITH_COMPLETED)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["edit", "my-task", "--set", "state", "done"])

    assert result.exit_code == 0, f"edit failed: {result.output}"

    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    stored = datetime.fromisoformat(str(tasks[0].metadata["completed"]))
    assert stored > datetime(2026, 6, 10, 12, tzinfo=timezone.utc)


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
    assert "completed" not in tasks[0].metadata, (
        "completed must not be injected when only editing an unrelated field"
    )


def test_edit_state_done_recurring_does_not_set_completed(tmp_path: Path, monkeypatch) -> None:
    """Recurring tasks transitioning to done reset to waiting and must not get completed."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(RECURRING_TASK)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["edit", "my-task", "--set", "state", "done"])

    assert result.exit_code == 0, f"edit failed: {result.output}"

    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    task = tasks[0]
    # Recurring tasks reset to waiting — completed should not be stamped on reset
    assert task.metadata["state"] == "waiting", "recurring task must reset to waiting"
    assert "completed" not in task.metadata, (
        "recurring done tasks must not carry a completed stamp into the next cycle"
    )


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
    ("recur_value", "wait_survives"),
    [
        # Malformed — not a recurrence at all, so the task is terminal in every
        # sense and its stale scheduling fields are cleaned.
        ("sometimes", False),
        # Cron — documented-valid (is_valid_recur_value accepts it) but
        # parse_recur_interval() returns None, so gptodo cannot compute the next
        # fire date. wait: is owned by the external scheduler and must survive.
        ("0 9 * * 1", True),
    ],
)
def test_uncomputable_recur_is_terminal_and_gets_completed(
    tmp_path: Path, monkeypatch, recur_value: str, wait_survives: bool
) -> None:
    """recur: values the parser cannot compute are terminal, so they get stamped.

    The recurrence handler resets a task to todo only when
    ``parse_recur_interval()`` returns an interval; everything else (malformed
    strings *and* cron expressions, which are documented-valid but not yet
    computed) is left sitting in done. The stamp predicate must agree, or such
    tasks end up permanently done with no completed stamp.

    Stale-field cleanup, however, must *not* agree: a cron recur: is a real
    recurrence whose next-fire date lives in wait:, maintained by whatever
    external scheduler owns the cron. Deleting it on `--set state done` destroys
    that scheduler's state irrecoverably.
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(
        RECURRING_TASK.replace("recur: 7d", f"recur: {recur_value!r}")
        + "next_action: run the export\n"
    )

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["edit", "my-task", "--set", "state", "done"])

    assert result.exit_code == 0, result.output
    task = load_tasks(tasks_dir)[0]
    assert task.metadata["state"] == "done", "uncomputable recur must not reset to waiting"
    assert "completed" in task.metadata
    if wait_survives:
        assert task.metadata.get("wait"), (
            "a cron recur: is a valid recurrence whose next-fire date lives in "
            "wait:; marking it done must not delete the external scheduler's state"
        )
    else:
        assert "wait" not in task.metadata, "terminal tasks must not keep a stale wait:"


def test_cron_recur_done_preserves_scheduling_fields(tmp_path: Path, monkeypatch) -> None:
    """All four stale-cleanup fields survive `done` on a cron-recurring task.

    Regression guard: the terminal predicate was once gated on
    ``parse_recur_interval()``, which returns None for cron, so marking a
    cron task done silently stripped next_action/waiting_for/waiting_since/wait.
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(
        """\
---
state: active
created: 2026-06-16T00:00:00+00:00
recur: "0 9 * * 1"
wait: 2026-09-01
next_action: run the weekly export
waiting_for: external scheduler
waiting_since: 2026-08-01
---
# Cron Task
"""
    )

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["edit", "my-task", "--set", "state", "done"])

    assert result.exit_code == 0, result.output
    task = load_tasks(tasks_dir)[0]
    assert task.metadata["state"] == "done"
    assert "completed" in task.metadata
    for field in ("wait", "next_action", "waiting_for", "waiting_since"):
        assert field in task.metadata, f"cron-recurring task lost {field} on done"


def test_recurring_reset_preserves_explicitly_set_completed(tmp_path: Path, monkeypatch) -> None:
    """`--set completed <value>` survives the recurrence reset.

    The stamp/clear pair documents that an explicit user value wins over the
    automation in either direction; the recurrence reset must honour the same
    contract instead of silently dropping the field the caller just set.
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(RECURRING_TASK)

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
            "2026-05-01T00:00:00+00:00",
        ],
    )

    assert result.exit_code == 0, result.output
    task = load_tasks(tasks_dir)[0]
    assert task.metadata["state"] == "waiting", "7d recur must still reset to waiting"
    assert str(task.metadata.get("completed")) == "2026-05-01T00:00:00+00:00", (
        "an explicit --set completed must not be silently deleted by the recurrence reset"
    )


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


def test_resaving_already_done_task_does_not_fabricate_completed(
    tmp_path: Path, monkeypatch
) -> None:
    """An idempotent re-save of an already-done task must not stamp "just now".

    ``--set state done`` on a task that is *already* done is a no-op transition,
    but the change is still recorded, so a post-edit-only check would stamp it.
    Legacy tasks closed before this feature shipped carry no ``completed``; a
    fabricated "completed just now" corrupts the very completion-duration and
    fast-close signals the stamp exists to measure.
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(ACTIVE_TASK.replace("state: active", "state: done"))

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["edit", "my-task", "--set", "state", "done"])

    assert result.exit_code == 0, result.output
    task = load_tasks(tasks_dir)[0]
    assert task.metadata["state"] == "done"
    assert "completed" not in task.metadata, (
        "re-saving an already-done task must not fabricate a completion time; "
        "only a real non-terminal → terminal transition stamps"
    )


@pytest.mark.parametrize(
    ("from_state", "to_state", "extra_frontmatter"),
    [
        ("waiting", "active", "waiting_for: someone\n"),
        ("backlog", "todo", ""),
    ],
)
def test_forward_transition_preserves_completed(
    tmp_path: Path, monkeypatch, from_state: str, to_state: str, extra_frontmatter: str
) -> None:
    """Ordinary forward transitions must not delete a ``completed`` the task carries.

    The reopen-clear exists for terminal → open only. Keyed off the post-edit
    state alone it also fires on waiting → active / backlog → todo, silently
    dropping a stamp that was set manually or left from an earlier cycle.
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(
        ACTIVE_TASK_WITH_COMPLETED.replace("state: active", f"state: {from_state}").replace(
            "completed:", f"{extra_frontmatter}completed:"
        )
    )

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["edit", "my-task", "--set", "state", to_state])

    assert result.exit_code == 0, result.output
    task = load_tasks(tasks_dir)[0]
    assert task.metadata["state"] == to_state
    assert "completed" in task.metadata, (
        f"{from_state} → {to_state} is not a reopen; completed must survive"
    )
    stored = datetime.fromisoformat(str(task.metadata["completed"]))
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    assert stored == datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


def test_recurring_reset_removes_existing_completed(tmp_path: Path, monkeypatch) -> None:
    """The recurrence reset drops a stale stamp carried in from a previous cycle."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(RECURRING_TASK_WITH_COMPLETED)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["edit", "my-task", "--set", "state", "done"])

    assert result.exit_code == 0, result.output
    task = load_tasks(tasks_dir)[0]
    assert task.metadata["state"] == "waiting"
    assert "completed" not in task.metadata
