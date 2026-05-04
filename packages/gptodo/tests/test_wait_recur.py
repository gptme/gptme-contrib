"""Tests for wait: and recur: scheduling fields."""

from datetime import date, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.utils import (
    advance_wait,
    is_task_ready,
    load_tasks,
    parse_recur_interval,
    parse_wait_date,
    task_is_waiting_for_date,
)


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


# ---------------------------------------------------------------------------
# parse_wait_date
# ---------------------------------------------------------------------------


def test_parse_wait_date_string() -> None:
    assert parse_wait_date("2026-05-10") == date(2026, 5, 10)


def test_parse_wait_date_datetime_string() -> None:
    assert parse_wait_date("2026-05-10T09:00:00") == date(2026, 5, 10)


def test_parse_wait_date_date_object() -> None:
    d = date(2026, 5, 10)
    assert parse_wait_date(d) == d


def test_parse_wait_date_none() -> None:
    assert parse_wait_date(None) is None


def test_parse_wait_date_invalid() -> None:
    assert parse_wait_date("not-a-date") is None


# ---------------------------------------------------------------------------
# parse_recur_interval
# ---------------------------------------------------------------------------


def test_parse_recur_days() -> None:
    assert parse_recur_interval("7d") == timedelta(days=7)


def test_parse_recur_hours() -> None:
    assert parse_recur_interval("24h") == timedelta(hours=24)


def test_parse_recur_weekly() -> None:
    assert parse_recur_interval("weekly") == timedelta(days=7)


def test_parse_recur_monthly() -> None:
    assert parse_recur_interval("monthly") == timedelta(days=30)


def test_parse_recur_cron_returns_none() -> None:
    # cron expressions are accepted but not yet computed to a timedelta
    assert parse_recur_interval("0 9 * * 1") is None


# ---------------------------------------------------------------------------
# advance_wait
# ---------------------------------------------------------------------------


def test_advance_wait_from_future_date() -> None:
    future = date.today() + timedelta(days=3)
    result = advance_wait(future, "7d")
    assert result == future + timedelta(days=7)


def test_advance_wait_from_past_date() -> None:
    # Lapsed task: base should be today, not the stale past date
    past = date.today() - timedelta(days=10)
    result = advance_wait(past, "7d")
    assert result == date.today() + timedelta(days=7)


def test_advance_wait_from_none() -> None:
    result = advance_wait(None, "7d")
    assert result == date.today() + timedelta(days=7)


def test_advance_wait_sub_24h_not_today() -> None:
    # Sub-24h hour intervals must produce a future date, not today.
    # Python date + timedelta(hours=12) silently drops sub-day components and
    # returns today — advance_wait must guard against this.
    result = advance_wait(None, "12h")
    assert result > date.today(), "12h recurrence must schedule at least 1 day out"

    result6 = advance_wait(None, "6h")
    assert result6 > date.today(), "6h recurrence must schedule at least 1 day out"


# ---------------------------------------------------------------------------
# task_is_waiting_for_date
# ---------------------------------------------------------------------------


def test_task_is_waiting_future(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    future = (date.today() + timedelta(days=5)).isoformat()
    write_task(tasks_dir, "future-task", state="backlog", created="2026-01-01", wait=future)

    tasks = load_tasks(tasks_dir)
    task = next(t for t in tasks if t.name == "future-task")
    assert task_is_waiting_for_date(task) is True


def test_task_is_not_waiting_past(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    past = (date.today() - timedelta(days=1)).isoformat()
    write_task(tasks_dir, "past-task", state="backlog", created="2026-01-01", wait=past)

    tasks = load_tasks(tasks_dir)
    task = next(t for t in tasks if t.name == "past-task")
    assert task_is_waiting_for_date(task) is False


def test_task_is_not_waiting_today(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    today = date.today().isoformat()
    write_task(tasks_dir, "today-task", state="backlog", created="2026-01-01", wait=today)

    tasks = load_tasks(tasks_dir)
    task = next(t for t in tasks if t.name == "today-task")
    # wait == today means task becomes available today (not waiting)
    assert task_is_waiting_for_date(task) is False


# ---------------------------------------------------------------------------
# is_task_ready with wait:
# ---------------------------------------------------------------------------


def test_is_task_ready_blocked_by_future_wait(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    future = (date.today() + timedelta(days=5)).isoformat()
    write_task(tasks_dir, "sched-task", state="backlog", created="2026-01-01", wait=future)

    tasks = load_tasks(tasks_dir)
    task_lookup = {t.name: t for t in tasks}
    assert is_task_ready(task_lookup["sched-task"], task_lookup) is False


def test_is_task_ready_unblocked_when_wait_passed(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    past = (date.today() - timedelta(days=1)).isoformat()
    write_task(tasks_dir, "past-task", state="backlog", created="2026-01-01", wait=past)

    tasks = load_tasks(tasks_dir)
    task_lookup = {t.name: t for t in tasks}
    assert is_task_ready(task_lookup["past-task"], task_lookup) is True


# ---------------------------------------------------------------------------
# gptodo next skips future-wait tasks
# ---------------------------------------------------------------------------


def test_next_skips_future_wait_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    future = (date.today() + timedelta(days=7)).isoformat()
    write_task(tasks_dir, "future-task", state="backlog", created="2026-01-01", wait=future)
    write_task(tasks_dir, "ready-task", state="backlog", created="2026-01-01")

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["next", "--json"])

    assert result.exit_code == 0
    import json

    data = json.loads(result.output)
    assert data["next_task"] is not None
    assert data["next_task"]["id"] == "ready-task"


# ---------------------------------------------------------------------------
# gptodo edit --set state done on recurring task resets to todo
# ---------------------------------------------------------------------------


def test_edit_done_with_recur_resets_to_todo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    today = date.today().isoformat()
    write_task(
        tasks_dir,
        "weekly-review",
        state="todo",
        created="2026-01-01",
        wait=today,
        recur="7d",
    )

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["edit", "weekly-review", "--set", "state", "done"])

    assert result.exit_code == 0, result.output
    assert "recurring" in result.output.lower() or "reset" in result.output.lower()

    # Task should now be todo with a future wait date
    import frontmatter as fm

    post = fm.load(tasks_dir / "weekly-review.md")
    assert post.metadata["state"] == "todo"
    next_wait = date.fromisoformat(str(post.metadata["wait"]))
    assert next_wait > date.today()


def test_edit_done_without_recur_stays_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    write_task(tasks_dir, "one-off", state="todo", created="2026-01-01")

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["edit", "one-off", "--set", "state", "done"])

    assert result.exit_code == 0, result.output

    import frontmatter as fm

    post = fm.load(tasks_dir / "one-off.md")
    assert post.metadata["state"] == "done"
