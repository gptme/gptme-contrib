"""Tests for `gptodo status --compact` mode.

Compact mode should show backlog, todo, active, and ready_for_review tasks.
It must NOT hide todo/ready_for_review tasks (regression from the original
["backlog", "active"]-only filter).
"""

from pathlib import Path

from click.testing import CliRunner

from gptodo.cli import cli


def write_task(tasks_dir: Path, name: str, **metadata: object) -> None:
    lines = ["---"]
    for key, value in metadata.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")
    lines.extend(["---", f"# {name}"])
    (tasks_dir / f"{name}.md").write_text("\n".join(lines))


def _run_status_compact(tmp_path: Path, monkeypatch) -> str:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["status", "--compact"])
    assert result.exit_code == 0, result.output
    return result.output


def test_status_compact_includes_todo_tasks(tmp_path: Path, monkeypatch) -> None:
    """Compact mode must show todo tasks, not just backlog/active."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "backlog-task", state="backlog", created="2026-01-01T00:00:00")
    write_task(tasks_dir, "todo-task", state="todo", created="2026-01-01T00:00:00")
    write_task(tasks_dir, "active-task", state="active", created="2026-01-01T00:00:00")

    output = _run_status_compact(tmp_path, monkeypatch)

    assert "backlog-task" in output
    assert "todo-task" in output, "compact mode must show todo tasks"
    assert "active-task" in output


def test_status_compact_includes_ready_for_review_tasks(tmp_path: Path, monkeypatch) -> None:
    """Compact mode must show ready_for_review tasks."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "active-task", state="active", created="2026-01-01T00:00:00")
    write_task(tasks_dir, "rfr-task", state="ready_for_review", created="2026-01-01T00:00:00")

    output = _run_status_compact(tmp_path, monkeypatch)

    assert "active-task" in output
    assert "rfr-task" in output, "compact mode must show ready_for_review tasks"


def test_status_compact_excludes_terminal_states(tmp_path: Path, monkeypatch) -> None:
    """Compact mode must not show done, cancelled, or someday tasks."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "active-task", state="active", created="2026-01-01T00:00:00")
    write_task(tasks_dir, "done-task", state="done", created="2026-01-01T00:00:00")
    write_task(tasks_dir, "cancelled-task", state="cancelled", created="2026-01-01T00:00:00")
    write_task(tasks_dir, "someday-task", state="someday", created="2026-01-01T00:00:00")

    output = _run_status_compact(tmp_path, monkeypatch)

    assert "active-task" in output
    assert "done-task" not in output
    assert "cancelled-task" not in output
    assert "someday-task" not in output
