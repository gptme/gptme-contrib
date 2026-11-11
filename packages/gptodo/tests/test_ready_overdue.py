"""Tests for `gptodo ready --overdue`: blocked tasks whose wait: date has passed.

`task_has_waiting_blocker` keeps any human `waiting_for` and any `state: waiting`
blocked forever, so a `wait:` used as a re-check date never re-surfaces the task.
`--overdue` lists those tasks for re-checking without releasing them.
"""

import json
from datetime import date, timedelta
from pathlib import Path

from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.utils import load_tasks, task_wait_gate_passed

PAST = (date.today() - timedelta(days=3)).isoformat()
FUTURE = (date.today() + timedelta(days=3)).isoformat()


def write_task(tasks_dir: Path, name: str, **metadata: object) -> None:
    lines = ["---"]
    for key, value in metadata.items():
        lines.append(f"{key}: {value}")
    lines.extend(["---", f"# {name}"])
    (tasks_dir / f"{name}.md").write_text("\n".join(lines))


def _setup(tmp_path: Path) -> Path:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    base = {"created": "2026-03-28T00:00:00"}
    # Should be listed
    write_task(
        tasks_dir, "human-wait-passed", state="todo", wait=PAST, waiting_for="Q3 earnings", **base
    )
    write_task(tasks_dir, "waiting-state-passed", state="waiting", wait=PAST, **base)
    # Should not be listed
    write_task(
        tasks_dir, "human-wait-future", state="todo", wait=FUTURE, waiting_for="Q3 earnings", **base
    )
    write_task(tasks_dir, "waiting-no-wait", state="waiting", waiting_for="Erik", **base)
    write_task(tasks_dir, "plain-todo-passed", state="todo", wait=PAST, **base)
    write_task(tasks_dir, "done-passed", state="done", wait=PAST, waiting_for="x", **base)
    write_task(
        tasks_dir,
        "machine-recur-passed",
        state="waiting",
        wait=PAST,
        wait_kind="machine",
        **base,
    )
    return tasks_dir


def test_task_wait_gate_passed_predicate(tmp_path: Path) -> None:
    tasks = {t.name: t for t in load_tasks(_setup(tmp_path))}
    flagged = {name for name, t in tasks.items() if task_wait_gate_passed(t)}
    assert flagged == {"human-wait-passed", "waiting-state-passed"}


def test_ready_overdue_json(tmp_path: Path, monkeypatch) -> None:
    _setup(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["ready", "--overdue", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    names = [t["id"] for t in payload["overdue_tasks"]]
    assert sorted(names) == ["human-wait-passed", "waiting-state-passed"]
    assert payload["count"] == 2
    row = next(t for t in payload["overdue_tasks"] if t["waiting_for"])
    assert row["waiting_for"] == "Q3 earnings"
    assert row["wait"] == PAST


def test_ready_overdue_does_not_release(tmp_path: Path, monkeypatch) -> None:
    """Plain `ready` must still hide the blocked tasks."""
    _setup(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["ready", "--jsonl"])
    assert result.exit_code == 0
    names = {json.loads(line)["id"] for line in result.stdout.splitlines() if line.strip()}
    assert "human-wait-passed" not in names
    assert "waiting-state-passed" not in names
    assert "plain-todo-passed" in names


def test_ready_overdue_table(tmp_path: Path, monkeypatch) -> None:
    _setup(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["ready", "--overdue"])
    assert result.exit_code == 0
    assert "human-wait-passed" in result.output
    assert "human-wait-future" not in result.output


def test_ready_overdue_empty(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "t", state="todo", created="2026-03-28T00:00:00")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["ready", "--overdue", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == {"overdue_tasks": [], "count": 0}
