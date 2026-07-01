"""Tests for `gptodo expire` — auto-reap long-quiet tasks to unclutter the queue.

Gordon 2026-07-01: `expired` is a soft-terminal state auto-applied by
`gptodo expire` when a task has sat in an eligible state (backlog/todo/someday)
for longer than the expire window (default 90 days). Revival is a plain
`gptodo edit --set state <backlog|todo>` — no --force needed — because the
point is to unclutter, not to permanently kill.

These tests cover:

  1. Default 90d window reaps only tasks that meet ALL predicates
     (state, age, no recur, no future wait).
  2. --days N adjusts the window.
  3. --state restricts which eligible states get reaped.
  4. --dry-run reports but does not mutate.
  5. Recurring tasks (recur:) are never reaped even when old.
  6. Tasks with a future wait: date are never reaped even when old.
  7. active/waiting/ready_for_review/done/cancelled are never reaped
     regardless of age.
  8. Expired tasks stamp expired_from and expired_at.
  9. Revival: expired → backlog is a legal transition without --force.
 10. Auto-expire is idempotent — a second run on the same tree finds
     nothing new to expire.
 11. --json emits a stable machine-readable contract.
 12. Expired tasks are excluded from `gptodo ready` and `gptodo next`.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.utils import load_tasks


def _write(
    tasks_dir: Path,
    name: str,
    state: str,
    created: str,
    extra: str = "",
) -> None:
    """Write a task file with the given state, created date, and any extra frontmatter."""
    fm_lines = [f"state: {state}", f"created: {created}"]
    if extra.strip():
        fm_lines.append(extra.strip())
    (tasks_dir / f"{name}.md").write_text(
        "---\n" + "\n".join(fm_lines) + "\n---\n# " + name + "\n"
    )


def _iso(days_ago: int) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


# ---------- Basic reaping ------------------------------------------------------


def test_expire_reaps_stale_backlog_task(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "old", "backlog", _iso(100))

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["expire"])

    assert result.exit_code == 0, result.output
    tasks = load_tasks(tasks_dir)
    assert tasks[0].metadata["state"] == "expired"
    assert tasks[0].metadata["expired_from"] == "backlog"
    assert "expired_at" in tasks[0].metadata


def test_expire_leaves_fresh_task_untouched(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "fresh", "backlog", _iso(10))

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["expire"])

    assert result.exit_code == 0, result.output
    tasks = load_tasks(tasks_dir)
    assert tasks[0].metadata["state"] == "backlog"
    assert "expired_from" not in tasks[0].metadata


def test_expire_custom_days_window(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "middle-aged", "todo", _iso(40))

    monkeypatch.chdir(tmp_path)
    # 90d default would skip a 40d task; 30d window reaps it.
    result = CliRunner().invoke(cli, ["expire", "--days", "30"])

    assert result.exit_code == 0, result.output
    tasks = load_tasks(tasks_dir)
    assert tasks[0].metadata["state"] == "expired"
    assert tasks[0].metadata["expired_from"] == "todo"


def test_expire_multiple_task_states(tmp_path: Path, monkeypatch) -> None:
    """Default should reap backlog, todo, someday when old — nothing else."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "old-backlog", "backlog", _iso(100))
    _write(tasks_dir, "old-todo", "todo", _iso(100))
    _write(tasks_dir, "old-someday", "someday", _iso(100))
    _write(tasks_dir, "old-active", "active", _iso(200))
    _write(tasks_dir, "old-waiting", "waiting", _iso(200))
    _write(tasks_dir, "old-review", "ready_for_review", _iso(200))
    _write(tasks_dir, "old-done", "done", _iso(200))
    _write(tasks_dir, "old-cancelled", "cancelled", _iso(200))

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["expire"])
    assert result.exit_code == 0, result.output

    by_name = {t.name: t for t in load_tasks(tasks_dir)}
    assert by_name["old-backlog"].metadata["state"] == "expired"
    assert by_name["old-todo"].metadata["state"] == "expired"
    assert by_name["old-someday"].metadata["state"] == "expired"
    assert by_name["old-active"].metadata["state"] == "active"
    assert by_name["old-waiting"].metadata["state"] == "waiting"
    assert by_name["old-review"].metadata["state"] == "ready_for_review"
    assert by_name["old-done"].metadata["state"] == "done"
    assert by_name["old-cancelled"].metadata["state"] == "cancelled"


# ---------- --state filter -----------------------------------------------------


def test_expire_state_filter(tmp_path: Path, monkeypatch) -> None:
    """--state backlog should reap only backlog, leaving old todo/someday alone."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "old-backlog", "backlog", _iso(200))
    _write(tasks_dir, "old-todo", "todo", _iso(200))
    _write(tasks_dir, "old-someday", "someday", _iso(200))

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["expire", "--state", "backlog"])
    assert result.exit_code == 0, result.output

    by_name = {t.name: t for t in load_tasks(tasks_dir)}
    assert by_name["old-backlog"].metadata["state"] == "expired"
    assert by_name["old-todo"].metadata["state"] == "todo"
    assert by_name["old-someday"].metadata["state"] == "someday"


# ---------- --dry-run ----------------------------------------------------------


def test_expire_dry_run_does_not_mutate(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "old", "backlog", _iso(100))

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["expire", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Would expire" in result.output
    # File untouched
    tasks = load_tasks(tasks_dir)
    assert tasks[0].metadata["state"] == "backlog"


# ---------- Skip predicates ----------------------------------------------------


def test_expire_skips_recurring_tasks(tmp_path: Path, monkeypatch) -> None:
    """Recurring tasks are legitimately dormant between fires — never reap."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "weekly-review", "todo", _iso(365), extra="recur: 7d")

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["expire"])

    assert result.exit_code == 0, result.output
    tasks = load_tasks(tasks_dir)
    assert tasks[0].metadata["state"] == "todo"


def test_expire_skips_future_wait_tasks(tmp_path: Path, monkeypatch) -> None:
    """A task with wait: in the future is intentionally hidden, not stale."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    future = (date.today() + timedelta(days=30)).isoformat()
    _write(tasks_dir, "wait-until-later", "todo", _iso(200), extra=f"wait: {future}")

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["expire"])

    assert result.exit_code == 0, result.output
    tasks = load_tasks(tasks_dir)
    assert tasks[0].metadata["state"] == "todo"


def test_expire_reaps_tasks_with_past_wait(tmp_path: Path, monkeypatch) -> None:
    """A past-wait: date doesn't shield an otherwise-eligible stale task."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    past = (date.today() - timedelta(days=30)).isoformat()
    _write(tasks_dir, "wait-past", "backlog", _iso(200), extra=f"wait: {past}")

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["expire"])

    assert result.exit_code == 0, result.output
    tasks = load_tasks(tasks_dir)
    assert tasks[0].metadata["state"] == "expired"


# ---------- Revival ------------------------------------------------------------


def test_expired_task_can_be_revived_without_force(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "old", "backlog", _iso(100))

    monkeypatch.chdir(tmp_path)
    # Expire
    result = CliRunner().invoke(cli, ["expire"])
    assert result.exit_code == 0, result.output
    tasks = load_tasks(tasks_dir)
    assert tasks[0].metadata["state"] == "expired"

    # Revive to backlog — should just work, no --force
    result = CliRunner().invoke(cli, ["edit", "old", "--set", "state", "backlog"])
    assert result.exit_code == 0, result.output
    assert "illegal" not in result.output.lower()
    assert "refusing" not in result.output.lower()

    tasks = load_tasks(tasks_dir)
    assert tasks[0].metadata["state"] == "backlog"


def test_expired_task_can_be_revived_to_todo(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "old-todo", "todo", _iso(100))

    monkeypatch.chdir(tmp_path)
    CliRunner().invoke(cli, ["expire"])

    result = CliRunner().invoke(cli, ["edit", "old-todo", "--set", "state", "todo"])
    assert result.exit_code == 0, result.output
    tasks = load_tasks(tasks_dir)
    assert tasks[0].metadata["state"] == "todo"


# ---------- Idempotence --------------------------------------------------------


def test_expire_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    """Second run finds nothing new to expire (already-expired tasks stay put)."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "old", "backlog", _iso(100))

    monkeypatch.chdir(tmp_path)
    first = CliRunner().invoke(cli, ["expire", "--json"])
    assert first.exit_code == 0
    first_payload = json.loads(first.stdout)
    assert first_payload["count"] == 1

    second = CliRunner().invoke(cli, ["expire", "--json"])
    assert second.exit_code == 0
    second_payload = json.loads(second.stdout)
    assert second_payload["count"] == 0


# ---------- --json contract ----------------------------------------------------


def test_expire_json_output(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "old-a", "backlog", _iso(100))
    _write(tasks_dir, "old-b", "todo", _iso(200))

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["expire", "--json"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["count"] == 2
    assert payload["days_threshold"] == 90
    assert set(payload["eligible_states"]) == {"backlog", "todo", "someday"}
    assert payload["dry_run"] is False
    ids = {r["id"] for r in payload["expired"]}
    assert ids == {"old-a", "old-b"}
    from_states = {r["from_state"] for r in payload["expired"]}
    assert from_states == {"backlog", "todo"}


def test_expire_json_dry_run(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "old", "backlog", _iso(100))

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["expire", "--dry-run", "--json"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["dry_run"] is True

    # Not mutated
    tasks = load_tasks(tasks_dir)
    assert tasks[0].metadata["state"] == "backlog"


# ---------- --days validation --------------------------------------------------


def test_expire_rejects_nonpositive_days(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["expire", "--days", "0"])
    assert result.exit_code != 0
    assert "positive integer" in result.output


# ---------- Selection integration ---------------------------------------------


def test_expired_task_excluded_from_ready(tmp_path: Path, monkeypatch) -> None:
    """`gptodo ready` should not surface expired tasks."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "old", "backlog", _iso(100))
    _write(tasks_dir, "fresh", "backlog", _iso(1))

    monkeypatch.chdir(tmp_path)
    CliRunner().invoke(cli, ["expire"])

    result = CliRunner().invoke(cli, ["ready", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    ready_ids = {t["name"] for t in payload["ready_tasks"]}
    assert ready_ids == {"fresh"}
    assert "old" not in ready_ids


def test_expired_task_excluded_from_next(tmp_path: Path, monkeypatch) -> None:
    """`gptodo next` should not pick an expired task."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "old", "backlog", _iso(100))
    _write(tasks_dir, "fresh", "backlog", _iso(1))

    monkeypatch.chdir(tmp_path)
    CliRunner().invoke(cli, ["expire"])

    result = CliRunner().invoke(cli, ["next", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["next_task"] is not None
    assert payload["next_task"]["name"] == "fresh"


def test_expired_state_is_valid_state_choice_in_check(tmp_path: Path, monkeypatch) -> None:
    """An expired task should not trigger validation issues in `gptodo check`."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "old", "expired", _iso(100), extra="expired_from: backlog\nexpired_at: 2026-06-01")

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["check"])
    # check exits 1 only if there are issues; expired should be a clean state
    assert result.exit_code == 0, result.output
    assert "Invalid state" not in result.output


# ---------- expired_from / expired_at stamping --------------------------------


def test_expire_stamps_expired_from_matching_original_state(
    tmp_path: Path, monkeypatch
) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "old-someday", "someday", _iso(200))

    monkeypatch.chdir(tmp_path)
    CliRunner().invoke(cli, ["expire"])
    tasks = load_tasks(tasks_dir)
    assert tasks[0].metadata["state"] == "expired"
    assert tasks[0].metadata["expired_from"] == "someday"


def test_expire_stamps_expired_at_as_iso_date(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "old", "backlog", _iso(100))

    monkeypatch.chdir(tmp_path)
    CliRunner().invoke(cli, ["expire"])
    tasks = load_tasks(tasks_dir)
    at = str(tasks[0].metadata["expired_at"])
    # YYYY-MM-DD
    assert len(at) == 10
    datetime.strptime(at, "%Y-%m-%d")  # doesn't raise


# ---------- env var override --------------------------------------------------


def test_expire_env_var_overrides_default_days(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "middle-aged", "backlog", _iso(40))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GPTODO_EXPIRE_DAYS", "30")
    result = CliRunner().invoke(cli, ["expire"])
    assert result.exit_code == 0, result.output
    tasks = load_tasks(tasks_dir)
    assert tasks[0].metadata["state"] == "expired"
