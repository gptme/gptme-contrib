"""Tests for `gptodo status --compact` mode.

Compact mode should show backlog, todo, active, and ready_for_review tasks.
It must NOT hide todo/ready_for_review tasks (regression from the original
["backlog", "active"]-only filter).
"""

from datetime import datetime, timedelta
from pathlib import Path

from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.utils import format_time_ago


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


def test_status_compact_excludes_hidden_states(tmp_path: Path, monkeypatch) -> None:
    """Compact mode must not show waiting, done, cancelled, or someday tasks."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "active-task", state="active", created="2026-01-01T00:00:00")
    write_task(tasks_dir, "waiting-task", state="waiting", created="2026-01-01T00:00:00")
    write_task(tasks_dir, "done-task", state="done", created="2026-01-01T00:00:00")
    write_task(tasks_dir, "cancelled-task", state="cancelled", created="2026-01-01T00:00:00")
    write_task(tasks_dir, "someday-task", state="someday", created="2026-01-01T00:00:00")

    output = _run_status_compact(tmp_path, monkeypatch)

    assert "active-task" in output
    assert "waiting-task" not in output
    assert "done-task" not in output
    assert "cancelled-task" not in output
    assert "someday-task" not in output


def test_status_no_double_blank_under_header(tmp_path: Path, monkeypatch) -> None:
    """The 'Tasks Status' header must not be followed by two blank lines."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "active-task", state="active", created="2026-01-01T00:00:00")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["status", "--compact"])
    assert result.exit_code == 0, result.output
    # A double blank line renders as three consecutive newlines.
    assert "\n\n\n" not in result.output, "double blank line under header"


def test_status_summary_flag_shows_summary_once_and_no_listing(tmp_path: Path, monkeypatch) -> None:
    """--summary shows exactly one summary and omits the task listing (no duplicate)."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "active-task", state="active", created="2026-01-01T00:00:00")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["status", "--summary"])
    assert result.exit_code == 0, result.output
    assert result.output.count("Summary:") == 1, "summary must not be duplicated"
    assert "active-task" not in result.output, "--summary must not print the task listing"


def test_status_default_and_compact_have_single_summary(tmp_path: Path, monkeypatch) -> None:
    """Every mode prints the summary exactly once, never twice."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "active-task", state="active", created="2026-01-01T00:00:00")
    monkeypatch.chdir(tmp_path)
    for args in (["status"], ["status", "--compact"], ["status", "--compact", "--summary"]):
        result = CliRunner().invoke(cli, args)
        assert result.exit_code == 0, result.output
        assert result.output.count("Summary:") == 1, f"{args}: summary count"


def test_format_time_ago_date_only_uses_day_resolution() -> None:
    """Date-only (midnight) timestamps report day resolution, not fake hours."""
    now = datetime.now()
    today_midnight = datetime(now.year, now.month, now.day)
    assert format_time_ago(today_midnight) == "today"
    assert format_time_ago(today_midnight - timedelta(days=1)) == "yesterday"
    assert format_time_ago(today_midnight - timedelta(days=3)) == "3d ago"


def test_format_time_ago_with_time_component_keeps_precision() -> None:
    """Timestamps that carry a time-of-day keep hour/minute precision."""
    dt = datetime.now() - timedelta(hours=3, minutes=1, seconds=7)
    assert format_time_ago(dt) == "3h ago"
