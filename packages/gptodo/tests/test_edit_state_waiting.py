"""Tests for auto-injection of waiting_since when editing state to waiting.

When `gptodo edit --set state waiting` is called, `waiting_since` should be
automatically populated with the current UTC datetime if it is not already set.
This prevents the `validate-task-frontmatter` pre-commit hook from rejecting
commits that transition tasks to waiting state.
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

ALREADY_WAITING_TASK = """\
---
state: waiting
created: 2026-06-01T00:00:00+00:00
waiting_for: some-dependency
waiting_since: 2026-06-01
---
# Already Waiting Task
"""

WAITING_WITHOUT_SINCE = """\
---
state: waiting
created: 2026-06-01T00:00:00+00:00
waiting_for: some-dependency
---
# Pre-existing Waiting Task Without waiting_since
"""


def test_edit_state_waiting_auto_sets_waiting_since(tmp_path: Path, monkeypatch) -> None:
    """Transitioning to waiting auto-injects waiting_since as a UTC ISO datetime when waiting_for is set."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(ACTIVE_TASK)

    monkeypatch.chdir(tmp_path)
    before = datetime.now(timezone.utc).replace(microsecond=0)
    result = CliRunner().invoke(
        cli,
        ["edit", "my-task", "--set", "state", "waiting", "--set", "waiting_for", "some-blocker"],
    )
    after = datetime.now(timezone.utc)

    assert result.exit_code == 0, f"edit failed: {result.output}"

    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.metadata["state"] == "waiting"
    assert "waiting_since" in task.metadata, "waiting_since should be auto-set"
    injected = datetime.fromisoformat(str(task.metadata["waiting_since"]))
    if injected.tzinfo is None:
        injected = injected.replace(tzinfo=timezone.utc)
    assert (
        before <= injected <= after
    ), f"waiting_since {injected!r} not between {before!r} and {after!r}"


def test_edit_state_waiting_without_waiting_for_does_not_inject_waiting_since(
    tmp_path: Path, monkeypatch
) -> None:
    """Transitioning to waiting WITHOUT waiting_for must NOT inject waiting_since.

    Injecting waiting_since when waiting_for is absent would cause the pre-commit
    hook to reject the commit with 'waiting_since requires waiting_for'.
    """
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
    assert (
        "waiting_since" not in tasks[0].metadata
    ), "waiting_since must not be injected when waiting_for is absent"


def test_edit_unrelated_field_on_waiting_task_does_not_inject_waiting_since(
    tmp_path: Path, monkeypatch
) -> None:
    """Editing an unrelated field on a pre-existing waiting task must NOT inject waiting_since."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(WAITING_WITHOUT_SINCE)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli,
        ["edit", "my-task", "--set", "priority", "high"],
    )

    assert result.exit_code == 0, f"edit failed: {result.output}"

    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    assert (
        "waiting_since" not in tasks[0].metadata
    ), "waiting_since must not be injected when only editing an unrelated field"


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
            "waiting_for",
            "some-blocker",
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


def test_edit_clearing_waiting_for_does_not_inject_waiting_since(
    tmp_path: Path, monkeypatch
) -> None:
    """--set waiting_for none (clearing) must NOT trigger waiting_since injection.

    The any(...) guard must check value is not None so that clearing waiting_for
    is not mistaken for setting it.
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(ACTIVE_TASK)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli,
        ["edit", "my-task", "--set", "state", "waiting", "--set", "waiting_for", "none"],
    )

    assert result.exit_code == 0, f"edit failed: {result.output}"

    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    assert (
        "waiting_since" not in tasks[0].metadata
    ), "clearing waiting_for must not trigger waiting_since injection"
