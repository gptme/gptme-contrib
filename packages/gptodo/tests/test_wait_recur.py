"""Tests for wait: and recur: scheduling fields."""

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.utils import (
    advance_wait,
    is_task_ready,
    is_valid_recur_value,
    load_tasks,
    parse_recur_interval,
    parse_wait_date,
    task_has_waiting_blocker,
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


def test_is_valid_recur_value_accepts_cron() -> None:
    assert is_valid_recur_value("0 9 * * 1") is True


def test_is_valid_recur_value_rejects_garbage() -> None:
    assert is_valid_recur_value("weakly") is False


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


def test_advance_wait_sub_24h_returns_datetime() -> None:
    # Sub-24h intervals must return a *datetime* with exact precision so the task
    # is hidden for the right number of hours, not just "tomorrow".
    result = advance_wait(None, "12h")
    assert isinstance(result, datetime), "12h recurrence must return datetime"
    assert result > datetime.now(), "12h recurrence must be in the future"
    assert result < datetime.now() + timedelta(hours=13), "12h recurrence must not overshoot"

    result6 = advance_wait(None, "6h")
    assert isinstance(result6, datetime), "6h recurrence must return datetime"
    assert result6 > datetime.now(), "6h recurrence must be in the future"
    assert result6 < datetime.now() + timedelta(hours=7), "6h recurrence must not overshoot"


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
# gptodo edit --set state done on recurring task resets to waiting
# ---------------------------------------------------------------------------


def test_edit_done_with_recur_resets_to_waiting(
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

    # Task should now be waiting with a future wait date
    import frontmatter as fm

    post = fm.load(tasks_dir / "weekly-review.md")
    assert post.metadata["state"] == "waiting"
    next_wait = date.fromisoformat(str(post.metadata["wait"]))
    assert next_wait > date.today()


def test_recur_reset_strips_preexisting_waiting_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: recurring reset must replace a leftover human waiting_for.

    A prior human-wait (waiting_for: 'John to review') must not survive done→reset,
    or it stays a permanent blocker after the time gate expires. Replace it with a
    recurrence-gate string (and a fresh waiting_since) so the validator is happy
    and the auto-releaser still treats the task as a time gate.
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    today = date.today().isoformat()
    # Write a recurring task that already carries waiting_for from a prior human-wait state
    (tasks_dir / "human-wait-recur.md").write_text(
        f"---\n"
        f"state: active\n"
        f"created: 2026-01-01\n"
        f"wait: {today}\n"
        f"recur: 7d\n"
        f"waiting_for: John to review the PR\n"
        f"waiting_since: 2026-08-01T00:00:00+00:00\n"
        f"---\n"
        f"# human-wait-recur\n"
    )

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["edit", "human-wait-recur", "--set", "state", "done"])

    assert result.exit_code == 0, result.output

    import frontmatter as fm

    post = fm.load(tasks_dir / "human-wait-recur.md")
    assert post.metadata["state"] == "waiting", "recurring task must reset to waiting"
    assert post.metadata.get("wait_kind") == "machine"
    waiting_for = post.metadata.get("waiting_for", "")
    assert "John" not in waiting_for, "human leftover waiting_for must not survive reset"
    assert "recurrence gate" in waiting_for
    assert str(post.metadata["wait"]) in waiting_for
    waiting_since = str(post.metadata.get("waiting_since", ""))
    assert waiting_since, "state=waiting requires waiting_since"
    assert not waiting_since.startswith("2026-08-01"), "waiting_since must be refreshed"


def test_expired_recurrence_gate_surfaces_in_ready_and_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A generated recurrence description must not become a permanent blocker."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    expired = "2020-01-01T00:00:00+00:00"
    write_task(
        tasks_dir,
        "weekly-review",
        state="waiting",
        created="2026-01-01",
        recur="7d",
        wait=expired,
        wait_kind="machine",
        waiting_for=f"'next recurrence gate (wait: {expired})'",
        waiting_since="2026-01-01T00:00:00+00:00",
    )

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    ready = runner.invoke(cli, ["ready", "--json"])
    assert ready.exit_code == 0, ready.output
    assert {task["id"] for task in json.loads(ready.output)["ready_tasks"]} == {"weekly-review"}

    next_result = runner.invoke(cli, ["next", "--json"])
    assert next_result.exit_code == 0, next_result.output
    assert json.loads(next_result.output)["next_task"]["id"] == "weekly-review"


def test_stale_recurrence_description_stays_blocked(tmp_path: Path) -> None:
    """Only the generated description for the current wait: is releasable."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(
        tasks_dir,
        "weekly-review",
        state="waiting",
        wait="2020-01-01",
        wait_kind="machine",
        waiting_for="'next recurrence gate (wait: 2019-01-01)'",
        waiting_since="2026-01-01T00:00:00+00:00",
    )

    task = load_tasks(tasks_dir)[0]
    assert task_has_waiting_blocker(task)


def test_edit_done_with_subday_recur_stores_datetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Completing a task with recur: 12h stores a datetime wait (not a date)."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    today = date.today().isoformat()
    write_task(
        tasks_dir,
        "frequent-check",
        state="todo",
        created="2026-01-01",
        wait=today,
        recur="12h",
    )

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["edit", "frequent-check", "--set", "state", "done"])

    assert result.exit_code == 0, result.output

    import frontmatter as fm

    post = fm.load(tasks_dir / "frequent-check.md")
    assert post.metadata["state"] == "waiting"
    wait_val = str(post.metadata["wait"])
    assert (
        "T" in wait_val or " " in wait_val
    ), f"sub-24h recur should store a datetime string with time component, got: {wait_val!r}"
    # Verify it's actually in the future
    next_dt = datetime.fromisoformat(wait_val.replace(" ", "T"))
    assert next_dt > datetime.now(), "next wait must be in the future"


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


def test_load_tasks_allows_cron_recur_without_validation_issue(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(
        tasks_dir,
        "cron-review",
        state="todo",
        created="2026-01-01",
        recur="0 9 * * 1",
    )

    task = next(t for t in load_tasks(tasks_dir) if t.name == "cron-review")
    assert not any("recur must be a valid interval" in issue for issue in task.issues)


def test_load_tasks_rejects_invalid_recur_with_issue(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(
        tasks_dir,
        "bad-recur",
        state="todo",
        created="2026-01-01",
        recur="weakly",
    )

    task = next(t for t in load_tasks(tasks_dir) if t.name == "bad-recur")
    assert any("recur must be a valid interval" in issue for issue in task.issues)


def test_edit_set_recur_accepts_cron_expression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "cron-task", state="todo", created="2026-01-01")

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["edit", "cron-task", "--set", "recur", "0 9 * * 1"])

    assert result.exit_code == 0, result.output

    import frontmatter as fm

    post = fm.load(tasks_dir / "cron-task.md")
    assert post.metadata["recur"] == "0 9 * * 1"


def test_edit_set_recur_rejects_invalid_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "bad-task", state="todo", created="2026-01-01")

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["edit", "bad-task", "--set", "recur", "weakly"])

    assert result.exit_code == 0
    assert "Invalid recur format" in result.output

    import frontmatter as fm

    post = fm.load(tasks_dir / "bad-task.md")
    assert "recur" not in post.metadata


def test_task_is_waiting_timezone_aware(tmp_path: Path) -> None:
    """PyYAML parses 'wait: 2099-01-01T00:00:00+00:00' as a tz-aware datetime.
    Comparing against naive datetime.now() raises TypeError; use now(tz=...) instead."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    # PyYAML auto-parses this as a tz-aware datetime object
    write_task(
        tasks_dir, "tz-future", state="todo", created="2026-01-01", wait="2099-01-01T00:00:00+00:00"
    )
    write_task(
        tasks_dir, "tz-past", state="todo", created="2026-01-01", wait="2000-01-01T00:00:00+00:00"
    )

    tasks = {t.name: t for t in load_tasks(tasks_dir)}
    # Must not raise TypeError on comparison
    assert task_is_waiting_for_date(tasks["tz-future"]) is True
    assert task_is_waiting_for_date(tasks["tz-past"]) is False


def test_advance_wait_timezone_aware() -> None:
    """advance_wait must not raise TypeError when current_wait is tz-aware."""
    from datetime import timezone

    aware = datetime(2000, 1, 1, tzinfo=timezone.utc)
    result = advance_wait(aware, "7d")
    # Result should be tz-aware and in the future
    assert isinstance(result, datetime)
    assert result.tzinfo is not None
    assert result > datetime.now(tz=timezone.utc)


def test_machine_probe_only_task_stays_blocked(tmp_path: Path) -> None:
    """Regression: machine tasks with a probe but no wait: date must stay blocked.

    Before the fix, `wait_kind: machine` without a `wait:` field passed the
    `not task_is_waiting_for_date` check (since task.wait is None → returns False),
    causing task_has_waiting_blocker to return False and probe-only machine tasks
    to appear ready when they shouldn't be.
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "probe-task.md").write_text(
        "---\n"
        "state: waiting\n"
        "wait_kind: machine\n"
        "probe: 'gh run view 123 --repo owner/repo --json conclusion -q .conclusion'\n"
        "waiting_for: CI run #123 to complete\n"
        "waiting_since: 2026-08-18T00:00:00+00:00\n"
        "---\n"
        "# probe-task\n"
    )
    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    task = tasks[0]
    # Probe-only machine task must still be treated as blocked
    assert task_has_waiting_blocker(task), (
        "machine task with probe but no wait: date must be blocked "
        "(task.wait is None so only the date-gate path should unblock)"
    )
    assert not is_task_ready(task, {}), "probe-only machine task must not be ready"


def test_machine_probe_plus_expired_wait_stays_blocked(tmp_path: Path) -> None:
    """Regression: machine tasks with both a probe and an expired wait: must stay blocked.

    An expired time-gate alone should not unblock a task that also has a probe —
    the probe still needs to be resolved. Without the probe guard, task_has_waiting_blocker
    would return False and the task would surface as ready prematurely.
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "probe-wait-task.md").write_text(
        "---\n"
        "state: waiting\n"
        "wait_kind: machine\n"
        "wait: 2020-01-01T00:00:00+00:00\n"  # expired date
        "probe: 'gh run view 123 --repo owner/repo --json conclusion -q .conclusion'\n"
        "waiting_for: CI run #123 to complete\n"
        "waiting_since: 2026-08-18T00:00:00+00:00\n"
        "---\n"
        "# probe-wait-task\n"
    )
    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    task = tasks[0]
    # Probe + expired date: probe gate still pending, must stay blocked
    assert task_has_waiting_blocker(task), (
        "machine task with probe AND expired wait: date must remain blocked "
        "(expired time-gate alone must not bypass an unresolved probe)"
    )
    assert not is_task_ready(task, {}), "probe+expired-wait machine task must not be ready"


def test_machine_expired_wait_with_human_waiting_for_stays_blocked(tmp_path: Path) -> None:
    """Regression: machine tasks with an expired wait: and a human waiting_for must stay blocked.

    `waiting_for` signals a human-described blocker that must be resolved explicitly.
    An expired time gate alone must not bypass a waiting_for field — the task should
    only auto-surface once waiting_for is cleared, even if wait_kind is machine.
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "human-wait-task.md").write_text(
        "---\n"
        "state: waiting\n"
        "wait_kind: machine\n"
        "wait: 2020-01-01T00:00:00+00:00\n"  # expired date
        "waiting_for: John to review the PR\n"
        "waiting_since: 2026-08-18T00:00:00+00:00\n"
        "---\n"
        "# human-wait-task\n"
    )
    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    task = tasks[0]
    # Human waiting_for must keep the task blocked even when the time gate expired
    assert task_has_waiting_blocker(task), (
        "machine task with expired wait: but human waiting_for must remain blocked "
        "(waiting_for takes precedence over an expired time gate)"
    )
    assert not is_task_ready(task, {}), "machine task with human waiting_for must not be ready"


def test_recur_reset_clears_stale_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recurring reset must clear a stale probe field.

    A recurring task may previously have carried a machine probe (e.g. a CI check).
    On the done→waiting reset that probe is no longer relevant and must be removed.
    If kept, task_has_waiting_blocker evaluates the probe and the task never
    auto-surfaces after the new recurrence gate expires.
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    today = date.today().isoformat()
    (tasks_dir / "probe-recur.md").write_text(
        f"---\n"
        f"state: active\n"
        f"created: 2026-01-01\n"
        f"wait: {today}\n"
        f"recur: 7d\n"
        f"probe: 'gh run view 123 --repo owner/repo --json conclusion -q .conclusion'\n"
        f"wait_kind: machine\n"
        f"---\n"
        f"# probe-recur\n"
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["edit", "probe-recur", "--set", "state", "done"])
    assert result.exit_code == 0, result.output

    import frontmatter as fm

    post = fm.load(tasks_dir / "probe-recur.md")
    assert post.metadata["state"] == "waiting", "recurring task must reset to waiting"
    assert "probe" not in post.metadata, (
        "stale probe must be cleared on recurrence reset; "
        "a leftover probe prevents task_has_waiting_blocker from releasing the task"
    )


def test_set_wait_on_recurrence_gate_updates_waiting_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--set wait on a recurrence-gate waiting task must keep waiting_for in sync.

    After a recurring reset the task carries waiting_for="next recurrence gate (wait: <old>)".
    If the user postpones via --set wait <new>, waiting_for must be updated to the new date.
    Without this, task_has_waiting_blocker fails to match the recurrence-gate pattern on
    the new wait value and permanently traps the task even after the new gate expires.
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    old_wait = "2025-01-01"
    new_wait = "2030-06-15"
    (tasks_dir / "recur-gate-task.md").write_text(
        f"---\n"
        f"state: waiting\n"
        f"wait_kind: machine\n"
        f"wait: {old_wait}\n"
        f"waiting_for: 'next recurrence gate (wait: {old_wait})'\n"
        f"waiting_since: 2026-08-01T00:00:00+00:00\n"
        f"created: 2026-01-01\n"
        f"recur: 7d\n"
        f"---\n"
        f"# recur-gate-task\n"
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["edit", "recur-gate-task", "--set", "wait", new_wait])
    assert result.exit_code == 0, result.output

    import frontmatter as fm

    post = fm.load(tasks_dir / "recur-gate-task.md")
    assert str(post.metadata["wait"]).startswith(
        new_wait
    ), f"wait: must be updated to {new_wait}, got {post.metadata['wait']}"
    wf = post.metadata.get("waiting_for", "")
    assert new_wait in wf, (
        f"waiting_for must reference the new wait date {new_wait!r}; got {wf!r}. "
        "A stale waiting_for permanently traps the task after the new gate expires."
    )


def test_set_wait_and_state_waiting_together_updates_waiting_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Combined --set wait + --set state waiting must still sync waiting_for.

    The per-field wait-sync used to inspect metadata['state'] while applying
    wait, so `--set wait NEW --set state waiting` on a not-yet-waiting
    recurrence-gate task left waiting_for pointing at the old date. After the
    new wait expires, task_has_waiting_blocker treats that stale string as a
    human blocker and the task never auto-surfaces (gptme/gptme-contrib#1539).
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    old_wait = "2025-01-01"
    new_wait = "2030-06-15"
    (tasks_dir / "recur-gate-task.md").write_text(
        f"---\n"
        f"state: todo\n"
        f"wait_kind: machine\n"
        f"wait: {old_wait}\n"
        f"waiting_for: 'next recurrence gate (wait: {old_wait})'\n"
        f"waiting_since: 2026-08-01T00:00:00+00:00\n"
        f"created: 2026-01-01\n"
        f"recur: 7d\n"
        f"---\n"
        f"# recur-gate-task\n"
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "edit",
            "recur-gate-task",
            "--set",
            "wait",
            new_wait,
            "--set",
            "state",
            "waiting",
        ],
    )
    assert result.exit_code == 0, result.output

    import frontmatter as fm

    post = fm.load(tasks_dir / "recur-gate-task.md")
    assert post.metadata["state"] == "waiting"
    assert str(post.metadata["wait"]).startswith(
        new_wait
    ), f"wait: must be updated to {new_wait}, got {post.metadata['wait']}"
    wf = post.metadata.get("waiting_for", "")
    assert new_wait in wf, (
        f"waiting_for must reference the new wait date {new_wait!r}; got {wf!r}. "
        "A stale waiting_for permanently traps the task after the new gate expires."
    )


def test_last_set_wait_wins_in_waiting_for_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multiple --set wait in one edit must sync waiting_for to the LAST value.

    The apply loop overwrites wait in order, so the last --set wait is the
    effective date. The post-loop sync used to take the first match from
    `changes`, leaving waiting_for pointing at an earlier date while wait
    holds the later one. After that later date expires,
    task_has_waiting_blocker treats the stale string as a human blocker
    (gptme/gptme-contrib#1539).
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    old_wait = "2025-01-01"
    first_wait = "2030-06-15"
    last_wait = "2030-07-01"
    (tasks_dir / "recur-gate-task.md").write_text(
        f"---\n"
        f"state: waiting\n"
        f"wait_kind: machine\n"
        f"wait: {old_wait}\n"
        f"waiting_for: 'next recurrence gate (wait: {old_wait})'\n"
        f"waiting_since: 2026-08-01T00:00:00+00:00\n"
        f"created: 2026-01-01\n"
        f"recur: 7d\n"
        f"---\n"
        f"# recur-gate-task\n"
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "edit",
            "recur-gate-task",
            "--set",
            "wait",
            first_wait,
            "--set",
            "wait",
            last_wait,
        ],
    )
    assert result.exit_code == 0, result.output

    import frontmatter as fm

    post = fm.load(tasks_dir / "recur-gate-task.md")
    assert str(post.metadata["wait"]).startswith(
        last_wait
    ), f"wait: must be the last --set wait {last_wait}, got {post.metadata['wait']}"
    wf = post.metadata.get("waiting_for", "")
    assert (
        last_wait in wf
    ), f"waiting_for must reference the last wait date {last_wait!r}; got {wf!r}."
    assert (
        first_wait not in wf
    ), f"waiting_for must not keep the first --set wait {first_wait!r}; got {wf!r}."


def test_trailing_set_wait_none_releases_recurrence_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Last --set wait none must clear the generated recurrence blocker.

    `--set wait NEW --set wait none` pops wait. Leaving the generated
    waiting_for behind would make a state=waiting task permanently blocked
    without a date (gptme/gptme-contrib#1539).
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    old_wait = "2025-01-01"
    new_wait = "2030-06-15"
    old_wf = f"next recurrence gate (wait: {old_wait})"
    (tasks_dir / "recur-gate-task.md").write_text(
        f"---\n"
        f"state: waiting\n"
        f"wait_kind: machine\n"
        f"wait: {old_wait}\n"
        f"waiting_for: '{old_wf}'\n"
        f"waiting_since: 2026-08-01T00:00:00+00:00\n"
        f"created: 2026-01-01\n"
        f"recur: 7d\n"
        f"---\n"
        f"# recur-gate-task\n"
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "edit",
            "recur-gate-task",
            "--set",
            "wait",
            new_wait,
            "--set",
            "wait",
            "none",
        ],
    )
    assert result.exit_code == 0, result.output

    import frontmatter as fm

    post = fm.load(tasks_dir / "recur-gate-task.md")
    assert (
        "wait" not in post.metadata
    ), f"wait: must be cleared by trailing --set wait none, got {post.metadata.get('wait')!r}"
    assert post.metadata["state"] == "todo"
    assert "waiting_for" not in post.metadata
    assert "waiting_since" not in post.metadata
    assert "wait_kind" not in post.metadata


def test_set_wait_none_preserves_explicit_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--set wait none --set state done` must not overwrite the explicit state.

    The wait-none recurrence-gate release used to force state=todo after all
    --set ops, so an explicit `--set state done` (or cancelled) was lost
    (gptme/gptme-contrib#1539 P1 on e71b18de).
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    old_wait = "2025-01-01"
    old_wf = f"next recurrence gate (wait: {old_wait})"
    (tasks_dir / "recur-gate-task.md").write_text(
        f"---\n"
        f"state: waiting\n"
        f"wait_kind: machine\n"
        f"wait: {old_wait}\n"
        f"waiting_for: '{old_wf}'\n"
        f"waiting_since: 2026-08-01T00:00:00+00:00\n"
        f"created: 2026-01-01\n"
        f"---\n"
        f"# recur-gate-task\n"
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "edit",
            "recur-gate-task",
            "--set",
            "wait",
            "none",
            "--set",
            "state",
            "done",
        ],
    )
    assert result.exit_code == 0, result.output

    import frontmatter as fm

    post = fm.load(tasks_dir / "recur-gate-task.md")
    assert post.metadata["state"] == "done", (
        f"explicit --set state done must survive wait-none release, "
        f"got {post.metadata.get('state')!r}"
    )
    assert "wait" not in post.metadata
    assert "waiting_for" not in post.metadata
    assert "waiting_since" not in post.metadata
    assert "wait_kind" not in post.metadata


@pytest.mark.parametrize("start_state", ["done", "cancelled", "active"])
def test_set_wait_none_does_not_reopen_nonwaiting_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, start_state: str
) -> None:
    """`--set wait none` must not force todo on a non-waiting task.

    The wait-none release used to set state=todo whenever no explicit
    `--set state` was in the same edit, even if the task was already
    done/cancelled/active and merely carried leftover recurrence-gate
    metadata (gptme/gptme-contrib#1539 P1 on ecb15242).
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    old_wait = "2025-01-01"
    old_wf = f"next recurrence gate (wait: {old_wait})"
    (tasks_dir / "recur-gate-task.md").write_text(
        f"---\n"
        f"state: {start_state}\n"
        f"wait_kind: machine\n"
        f"wait: {old_wait}\n"
        f"waiting_for: '{old_wf}'\n"
        f"waiting_since: 2026-08-01T00:00:00+00:00\n"
        f"created: 2026-01-01\n"
        f"---\n"
        f"# recur-gate-task\n"
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["edit", "recur-gate-task", "--set", "wait", "none"],
    )
    assert result.exit_code == 0, result.output

    import frontmatter as fm

    post = fm.load(tasks_dir / "recur-gate-task.md")
    assert post.metadata["state"] == start_state, (
        f"--set wait none must not reopen/demote {start_state} to todo, "
        f"got {post.metadata.get('state')!r}"
    )
    assert "wait" not in post.metadata
    assert "waiting_for" not in post.metadata
    assert "waiting_since" not in post.metadata
    assert "wait_kind" not in post.metadata


def test_set_wait_on_nonwaiting_recurrence_gate_drops_waiting_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--set wait NEW --set state todo` must drop the generated waiting_for.

    If wait-sync only rewrites waiting_for when state stays waiting, a leftover
    recurrence-gate string on a todo task is treated as a human blocker and
    traps the task (same family as gptme/gptme-contrib#1539).
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    old_wait = "2025-01-01"
    new_wait = "2030-06-15"
    (tasks_dir / "recur-gate-task.md").write_text(
        f"---\n"
        f"state: waiting\n"
        f"wait_kind: machine\n"
        f"wait: {old_wait}\n"
        f"waiting_for: 'next recurrence gate (wait: {old_wait})'\n"
        f"waiting_since: 2026-08-01T00:00:00+00:00\n"
        f"created: 2026-01-01\n"
        f"---\n"
        f"# recur-gate-task\n"
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "edit",
            "recur-gate-task",
            "--set",
            "wait",
            new_wait,
            "--set",
            "state",
            "todo",
        ],
    )
    assert result.exit_code == 0, result.output

    import frontmatter as fm

    post = fm.load(tasks_dir / "recur-gate-task.md")
    assert post.metadata["state"] == "todo"
    assert str(post.metadata["wait"]).startswith(new_wait)
    assert "waiting_for" not in post.metadata
    assert "waiting_since" not in post.metadata
    assert "wait_kind" not in post.metadata
