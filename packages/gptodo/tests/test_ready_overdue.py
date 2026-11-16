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


def test_ready_overdue_no_tasks_json(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tasks").mkdir()
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["ready", "--overdue", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"overdue_tasks": [], "count": 0}


def test_ready_overdue_sorts_wait_instants(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    for name, wait in [
        ("later", "2026-01-03T00:00:00+00:00"),
        ("earlier", "2026-01-03T01:00:00+02:00"),
        ("date-only", "2026-01-01"),
        ("local-naive", "2026-01-01T12:00:00"),
    ]:
        write_task(tasks_dir, name, state="waiting", wait=wait, created="2026-01-01T00:00:00")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    for output in ["--json", "--jsonl"]:
        result = runner.invoke(cli, ["ready", "--overdue", output])
        assert result.exit_code == 0, result.output
        rows = (
            json.loads(result.stdout)["overdue_tasks"]
            if output == "--json"
            else [json.loads(line) for line in result.stdout.splitlines()]
        )
        assert [row["id"] for row in rows] == ["date-only", "local-naive", "earlier", "later"]


def test_ready_overdue_skip_claimed(tmp_path: Path, monkeypatch) -> None:
    _setup(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("gptodo.cli._get_claimed_task_ids", lambda root: {"human-wait-passed"})
    result = CliRunner().invoke(cli, ["ready", "--overdue", "--skip-claimed", "--json"])
    assert result.exit_code == 0, result.output
    assert [row["id"] for row in json.loads(result.stdout)["overdue_tasks"]] == [
        "waiting-state-passed"
    ]


def test_ready_overdue_table_literal_blocker(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(
        tasks_dir,
        "literal-blocker",
        state="waiting",
        wait=PAST,
        waiting_for="'Confirm [/approval]'",
        created="2026-01-01T00:00:00",
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["ready", "--overdue"], terminal_width=140)
    assert result.exit_code == 0, result.exception
    assert "Confirm [/approval]" in result.output


def test_ready_overdue_lists_past_gate_deadline(tmp_path: Path, monkeypatch) -> None:
    """A task gated only by a typed gate.deadline is overdue once it passes."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    base = {"created": "2026-03-28T00:00:00"}
    write_task(
        tasks_dir,
        "gate-passed",
        state="waiting",
        gate="{kind: time, deadline: 2026-01-02T00:00:00+00:00}",
        **base,
    )
    write_task(
        tasks_dir,
        "gate-future",
        state="waiting",
        gate="{kind: time, deadline: 2999-01-01T00:00:00+00:00}",
        **base,
    )
    write_task(tasks_dir, "wait-passed", state="waiting", wait="2026-01-01", **base)
    write_task(tasks_dir, "wait-later", state="waiting", wait="2026-01-03", **base)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["ready", "--overdue", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)["overdue_tasks"]
    assert [r["id"] for r in rows] == ["wait-passed", "gate-passed", "wait-later"]
    gate_row = rows[1]
    assert gate_row["gated_by"] == "gate.deadline"
    assert gate_row["gate_kind"] == "time"
    assert gate_row["gate_deadline"].startswith("2026-01-02")
    assert rows[0]["gated_by"] == "wait"
    assert rows[0]["gate_kind"] is None


def test_ready_overdue_uses_earliest_passed_date(tmp_path: Path, monkeypatch) -> None:
    """Tasks with both passed dates appear once, ordered by the earlier instant."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    for name, wait, deadline in [
        ("gate-earlier", "2026-01-06T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        ("wait-earlier", "2026-01-02T00:00:00+00:00", "2026-01-04T00:00:00+00:00"),
        ("middle", "2026-01-03T00:00:00+00:00", "2026-01-05T00:00:00+00:00"),
    ]:
        write_task(
            tasks_dir,
            name,
            state="waiting",
            created="2026-01-01T00:00:00",
            wait=wait,
            gate=f"{{kind: time, deadline: {deadline}}}",
        )
    monkeypatch.chdir(tmp_path)
    for output in ["--json", "--jsonl"]:
        result = CliRunner().invoke(cli, ["ready", "--overdue", output])
        assert result.exit_code == 0, result.output
        if output == "--json":
            payload = json.loads(result.stdout)
            assert payload["count"] == 3
            rows = payload["overdue_tasks"]
        else:
            rows = [json.loads(line) for line in result.stdout.splitlines()]
        assert [row["id"] for row in rows] == ["gate-earlier", "wait-earlier", "middle"]
        assert [row["gated_by"] for row in rows] == ["gate.deadline", "wait", "wait"]
        assert rows[0]["gate_kind"] == "time"
        assert rows[0]["gate_deadline"] == "2026-01-01T00:00:00+00:00"
        assert rows[1]["gate_kind"] is None
        assert rows[1]["gate_deadline"] is None


def test_ready_overdue_table_shows_gate_deadline(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(
        tasks_dir,
        "gate-passed",
        state="waiting",
        created="2026-03-28T00:00:00",
        gate="{kind: time, deadline: 2026-01-02T00:00:00+00:00}",
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["ready", "--overdue"])
    assert result.exit_code == 0, result.output
    assert "gate-passed" in result.output
    assert "gate:time" in result.output
