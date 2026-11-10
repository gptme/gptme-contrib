"""Tests for stripping stale actionable/blocker metadata on terminal transitions.

When `gptodo edit --set state done|cancelled` lands a task in a terminal state,
the now-stale `next_action`, `waiting_for`, `waiting_since`, and `wait` fields
should be removed (TASKS.md best-practice #7). `tracking_issue` and
`upstream_coordination_id` are preserved for permanent traceability, and
recurring tasks (which reset to waiting) keep their fields.
"""

from __future__ import annotations

from pathlib import Path

import pytest
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
    """Recurring tasks reset to waiting and must retain next_action."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "recurring.md").write_text(RECURRING_TASK)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["edit", "recurring", "--set", "state", "done"])
    assert result.exit_code == 0, result.output

    meta = _meta(tasks_dir, "recurring")
    # Recur logic resets to waiting and keeps next_action.
    assert meta["state"] == "waiting"
    assert meta["next_action"] == "Do the recurring thing"


WAITING_WITH_CUMULATIVE_HISTORY = """\
---
state: waiting
created: 2026-06-01T00:00:00+00:00
waiting_for: some-dependency
waiting_since: 2026-06-10
first_waiting_since: 2026-05-01
waiting_spell_count: 3
---
# Waiting Task
"""

OPEN_WITH_CUMULATIVE_HISTORY = """\
---
state: active
created: 2026-06-01T00:00:00+00:00
first_waiting_since: 2026-05-01
waiting_spell_count: 3
---
# Open Task
"""


def test_done_strips_cumulative_waiting_history(tmp_path: Path, monkeypatch) -> None:
    """first_waiting_since/waiting_spell_count are cleared on terminal states."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(WAITING_WITH_CUMULATIVE_HISTORY)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["edit", "my-task", "--set", "state", "done"])
    assert result.exit_code == 0, result.output

    meta = _meta(tasks_dir, "my-task")
    assert meta["state"] == "done"
    assert "first_waiting_since" not in meta
    assert "waiting_spell_count" not in meta


def test_cumulative_waiting_fields_accept_set_none(tmp_path: Path, monkeypatch) -> None:
    """Both fields are registered, so `--set <field> none` clears them by hand."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(OPEN_WITH_CUMULATIVE_HISTORY)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["edit", "my-task", "--set", "first_waiting_since", "none"])
    assert result.exit_code == 0, result.output
    assert "first_waiting_since" not in _meta(tasks_dir, "my-task")

    result = CliRunner().invoke(cli, ["edit", "my-task", "--set", "waiting_spell_count", "none"])
    assert result.exit_code == 0, result.output
    assert "waiting_spell_count" not in _meta(tasks_dir, "my-task")


def test_invalid_date_value_hints_none_clears_field(tmp_path: Path, monkeypatch) -> None:
    """A rejected date value names the documented clear path instead of a dead end.

    Two friction vents reported "gptodo edit rejects both fields" after trying
    `--set first_waiting_since ""`; the error now points at `none`.
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(OPEN_WITH_CUMULATIVE_HISTORY)
    monkeypatch.chdir(tmp_path)

    before = _meta(tasks_dir, "my-task")

    result = CliRunner().invoke(cli, ["edit", "my-task", "--set", "first_waiting_since", ""])
    flat = " ".join(result.output.split())  # rich wraps at 80 columns
    assert "Invalid first_waiting_since date format" in flat
    assert "'none' to clear the field" in flat
    # The rejected edit must leave the field's value untouched, not just the key
    # present — compare the full metadata so a partial/incorrect write is caught.
    assert _meta(tasks_dir, "my-task") == before

    result = CliRunner().invoke(cli, ["edit", "my-task", "--set", "waiting_since", "not-a-date"])
    flat = " ".join(result.output.split())
    assert "Invalid waiting_since format" in flat
    assert "'none' to clear the field" in flat
    assert _meta(tasks_dir, "my-task") == before


DONE_TASK = """\
---
state: done
created: 2026-06-01T00:00:00+00:00
completed: 2026-06-02T00:00:00+00:00
priority: medium
---
# Done Task
"""


def test_explicit_stale_field_on_terminal_task_is_refused(tmp_path: Path, monkeypatch) -> None:
    """Setting a field the terminal strip would drop must fail loudly, not report success."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(DONE_TASK)
    monkeypatch.chdir(tmp_path)
    before = (tasks_dir / "my-task.md").read_text()

    for args in (
        ["--set", "next_action", "Do more"],
        ["--set", "waiting_for", "someone"],
        ["--set", "priority", "high", "--set", "wait", "2026-12-01"],
    ):
        result = CliRunner().invoke(cli, ["edit", "my-task", *args])
        assert result.exit_code == 1, (args, result.output)
        assert "Refusing to set" in result.output
        assert (tasks_dir / "my-task.md").read_text() == before


def test_closing_edit_with_explicit_next_action_is_refused(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(ACTIVE_WITH_NEXT_ACTION)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli, ["edit", "my-task", "--set", "state", "done", "--set", "next_action", "Later"]
    )
    assert result.exit_code == 1, result.output
    assert _meta(tasks_dir, "my-task")["state"] == "active"


@pytest.mark.parametrize("state", ["done", "cancelled"])
@pytest.mark.parametrize("clear_last", [True, False])
def test_terminal_field_repeated_sets_use_last_assignment(
    tmp_path: Path, monkeypatch, state: str, clear_last: bool
) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    path = tasks_dir / "my-task.md"
    path.write_text(DONE_TASK.replace("state: done", f"state: {state}"))
    monkeypatch.chdir(tmp_path)
    before = path.read_bytes()
    values = ["Later", "none"] if clear_last else ["none", "Later"]

    result = CliRunner().invoke(
        cli,
        [
            "edit",
            "my-task",
            "--set",
            "next_action",
            values[0],
            "--set",
            "next_action",
            values[1],
            "--set",
            "priority",
            "high",
        ],
    )

    if clear_last:
        assert result.exit_code == 0, result.output
        meta = _meta(tasks_dir, "my-task")
        assert meta["priority"] == "high"
        assert "next_action" not in meta
    else:
        assert result.exit_code == 1, result.output
        assert "Refusing to set" in result.output
        assert path.read_bytes() == before


def test_clearing_stale_field_on_terminal_task_still_allowed(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(DONE_TASK)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli, ["edit", "my-task", "--set", "next_action", "none", "--set", "priority", "high"]
    )
    assert result.exit_code == 0, result.output
    assert _meta(tasks_dir, "my-task")["priority"] == "high"
