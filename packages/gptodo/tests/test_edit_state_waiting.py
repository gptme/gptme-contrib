"""Tests for auto-injection of waiting_since when editing state to waiting.

When `gptodo edit --set state waiting` is called, `waiting_since` should be
automatically populated with today's date if it is not already present.
This prevents the pre-commit `validate-task-frontmatter` hook from rejecting
commits that transition tasks to waiting state.
"""

from __future__ import annotations

from datetime import date
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

ALREADY_WAITING_TASK = """\
---
state: waiting
created: 2026-06-01T00:00:00+00:00
waiting_for: some-dependency
waiting_since: 2026-06-01
---
# Already Waiting Task
"""


def test_edit_state_waiting_auto_sets_waiting_since(tmp_path: Path, monkeypatch) -> None:
    """Transitioning to waiting auto-injects waiting_since with today's date."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(ACTIVE_TASK)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli,
        ["edit", "my-task", "--set", "state", "waiting"],
    )

    assert result.exit_code == 0, f"edit failed: {result.output}"

    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.metadata["state"] == "waiting"
    assert "waiting_since" in task.metadata, "waiting_since should be auto-set"
    assert task.metadata["waiting_since"] == date.today().isoformat()


def test_edit_state_waiting_does_not_override_existing_waiting_since(
    tmp_path: Path, monkeypatch
) -> None:
    """If waiting_since is already set, it should not be overwritten."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(ALREADY_WAITING_TASK)

    monkeypatch.chdir(tmp_path)
    # Re-set to waiting (no-op transition, but tests the guard)
    result = CliRunner().invoke(
        cli,
        ["edit", "my-task", "--set", "state", "waiting"],
    )

    assert result.exit_code == 0, f"edit failed: {result.output}"

    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    # YAML parses bare date strings as datetime.date objects; compare via str()
    assert (
        str(tasks[0].metadata["waiting_since"]) == "2026-06-01"
    ), "pre-existing date must not change"


def test_edit_state_waiting_explicit_waiting_since_wins(tmp_path: Path, monkeypatch) -> None:
    """Explicit --set waiting_since takes precedence over auto-injection."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(ACTIVE_TASK)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "edit",
            "my-task",
            "--set",
            "state",
            "waiting",
            "--set",
            "waiting_since",
            "2026-06-10",
        ],
    )

    assert result.exit_code == 0, f"edit failed: {result.output}"

    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    # Explicit date stored as date string; YAML may parse back as datetime.date
    assert str(tasks[0].metadata["waiting_since"]) == "2026-06-10", "explicit date should win"
