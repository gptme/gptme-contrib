"""Regression tests for ready/next task selection."""

import json
from pathlib import Path

from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.utils import is_task_ready, load_tasks


def write_task(tasks_dir: Path, name: str, **metadata: object) -> None:
    """Write a minimal task file with YAML frontmatter."""
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


def test_is_task_ready_false_when_waiting_for_set(tmp_path: Path) -> None:
    """Tasks with waiting_for must not count as ready work."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(
        tasks_dir,
        "waiting-task",
        state="backlog",
        created="2026-03-28T00:00:00",
        waiting_for="Erik review",
    )

    tasks = load_tasks(tasks_dir)
    task_lookup = {task.name: task for task in tasks}

    assert is_task_ready(task_lookup["waiting-task"], task_lookup) is False


def test_ready_command_skips_waiting_for_tasks(tmp_path: Path, monkeypatch) -> None:
    """`gptodo ready` should not surface waiting tasks as actionable."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(
        tasks_dir,
        "waiting-task",
        state="backlog",
        created="2026-03-28T00:00:00",
        waiting_for="Erik review",
    )

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["ready", "--state", "backlog", "--json"])

    assert result.exit_code == 0
    payload_text = result.output.split("\nNo ready tasks found", 1)[0]
    payload = json.loads(payload_text)
    assert payload["count"] == 0
    assert payload["ready_tasks"] == []


def test_next_command_ignores_waiting_task_even_if_higher_priority(
    tmp_path: Path, monkeypatch
) -> None:
    """`gptodo next` should pick the real ready task, not the waiting one."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(
        tasks_dir,
        "waiting-task",
        state="backlog",
        created="2026-03-28T00:00:00",
        priority="high",
        waiting_for="Erik review",
    )
    write_task(
        tasks_dir,
        "ready-task",
        state="active",
        created="2026-03-28T01:00:00",
        priority="medium",
    )

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["next", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["next_task"]["id"] == "ready-task"
