"""Tests for `gptodo explain` command — per-task readiness diagnosis."""

from pathlib import Path

from click.testing import CliRunner

from gptodo.cli import cli


def write_task(tasks_dir: Path, name: str, **metadata: object) -> None:
    """Write a minimal task file with YAML frontmatter."""
    import yaml

    lines = ["---"]
    for key, value in metadata.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            # Use yaml.dump for proper quoting (handles #, : etc)
            dumped = yaml.dump({key: value}, default_flow_style=False).strip()
            lines.append(dumped)
    lines.extend(["---", f"# {name}"])
    (tasks_dir / f"{name}.md").write_text("\n".join(lines))


def run_explain(tmp_path: Path, task_id: str) -> str:
    """Run `gptodo explain <task_id>` from tmp_path and return output."""
    runner = CliRunner()
    result = runner.invoke(cli, ["explain", task_id], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return result.output


def test_explain_ready_task(tmp_path: Path, monkeypatch) -> None:
    """A simple todo task with no deps should report READY."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "my-task", state="todo", created="2026-01-01T00:00:00")
    monkeypatch.chdir(tmp_path)

    out = run_explain(tmp_path, "my-task")

    assert "✓ state" in out
    assert "✓ waiting_for" in out
    assert "VERDICT: READY" in out


def test_explain_waiting_task(tmp_path: Path, monkeypatch) -> None:
    """A task in waiting state should report NOT READY with waiting_for detail."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(
        tasks_dir,
        "blocked-task",
        state="waiting",
        created="2026-01-01T00:00:00",
        waiting_for="PR #123 merged",
    )
    monkeypatch.chdir(tmp_path)

    out = run_explain(tmp_path, "blocked-task")

    assert "✗" in out
    assert "waiting_for" in out
    assert "PR #123 merged" in out
    assert "VERDICT: NOT READY" in out


def test_explain_done_task(tmp_path: Path, monkeypatch) -> None:
    """A terminal (done) task should report NOT READY immediately."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "finished-task", state="done", created="2026-01-01T00:00:00")
    monkeypatch.chdir(tmp_path)

    out = run_explain(tmp_path, "finished-task")

    assert "terminal" in out
    assert "VERDICT: NOT READY" in out


def test_explain_someday_task(tmp_path: Path, monkeypatch) -> None:
    """A someday task should report NOT READY (deferred)."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "deferred-task", state="someday", created="2026-01-01T00:00:00")
    monkeypatch.chdir(tmp_path)

    out = run_explain(tmp_path, "deferred-task")

    assert "someday" in out
    assert "VERDICT: NOT READY" in out


def test_explain_blocked_by_dependency(tmp_path: Path, monkeypatch) -> None:
    """A task with an incomplete dependency should report NOT READY with dep info."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "prereq", state="todo", created="2026-01-01T00:00:00")
    write_task(
        tasks_dir,
        "depends-on-prereq",
        state="backlog",
        created="2026-01-01T00:00:00",
        requires=["prereq"],
    )
    monkeypatch.chdir(tmp_path)

    out = run_explain(tmp_path, "depends-on-prereq")

    assert "dependencies" in out
    assert "prereq" in out
    assert "VERDICT: NOT READY" in out


def test_explain_resolved_dependency(tmp_path: Path, monkeypatch) -> None:
    """A task whose dependency is done should report READY."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "prereq-done", state="done", created="2026-01-01T00:00:00")
    write_task(
        tasks_dir,
        "unblocked-task",
        state="backlog",
        created="2026-01-01T00:00:00",
        requires=["prereq-done"],
    )
    monkeypatch.chdir(tmp_path)

    out = run_explain(tmp_path, "unblocked-task")

    assert "✓ dependencies" in out
    assert "VERDICT: READY" in out


def test_explain_missing_task(tmp_path: Path, monkeypatch) -> None:
    """A task ID not found should give a clear error and exit with code 1."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    # Add a real task so we hit the "not found" branch, not "no tasks found"
    write_task(tasks_dir, "other-task", state="todo", created="2026-01-01T00:00:00")
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["explain", "nonexistent-task"])

    assert result.exit_code == 1
    assert "not found" in result.output.lower()


def test_explain_waiting_for_on_non_waiting_state(tmp_path: Path, monkeypatch) -> None:
    """A backlog task with waiting_for set should report NOT READY."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(
        tasks_dir,
        "sneaky-blocked",
        state="backlog",
        created="2026-01-01T00:00:00",
        waiting_for="external thing",
    )
    monkeypatch.chdir(tmp_path)

    out = run_explain(tmp_path, "sneaky-blocked")

    assert "waiting_for" in out
    assert "external thing" in out
    assert "VERDICT: NOT READY" in out


def test_explain_ready_for_review_skips_dep_checks(tmp_path: Path, monkeypatch) -> None:
    """ready_for_review tasks should show READY even with an unresolved dependency.

    gptodo ready deliberately skips dependency checks for ready_for_review tasks
    (work is done; only waiting_for matters). explain must match that behaviour so
    the diagnostic doesn't contradict the command it diagnoses.
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "open-prereq", state="todo", created="2026-01-01T00:00:00")
    write_task(
        tasks_dir,
        "rfr-task",
        state="ready_for_review",
        created="2026-01-01T00:00:00",
        requires=["open-prereq"],
    )
    monkeypatch.chdir(tmp_path)

    out = run_explain(tmp_path, "rfr-task")

    # Dep check should be skipped (not flagged as a blocker)
    assert "skipped" in out
    assert "VERDICT: READY" in out


def test_explain_ready_for_review_with_future_wait_date(tmp_path: Path, monkeypatch) -> None:
    """ready_for_review tasks with a future wait: date should still report READY.

    gptodo ready --state ready_for_review only checks waiting_for, not the wait
    date gate. explain must match: a future wait: date must not flip the verdict.
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(
        tasks_dir,
        "rfr-future-wait",
        state="ready_for_review",
        created="2026-01-01T00:00:00",
        wait="2099-12-31T00:00:00+00:00",
    )
    monkeypatch.chdir(tmp_path)

    out = run_explain(tmp_path, "rfr-future-wait")

    # Wait date check must be skipped — a future wait should NOT block READY
    assert "skipped" in out
    assert "VERDICT: READY" in out


def test_explain_empty_tasks_dir_exits_nonzero(tmp_path: Path, monkeypatch) -> None:
    """An empty tasks directory must exit with code 1 so scripts can detect failure."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["explain", "anything"])

    assert result.exit_code == 1
    assert "no tasks found" in result.output.lower()


def test_explain_frontier_pool_ready_for_review_not_ready(tmp_path: Path, monkeypatch) -> None:
    """A ready_for_review task in the frontier pool should report NOT READY.

    gptodo ready applies the pool filter before readiness checks, even for
    ready_for_review tasks. A frontier-pool task is excluded by the default
    general-pool filter, so explain must report NOT READY to match.
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(
        tasks_dir,
        "frontier-rfr",
        state="ready_for_review",
        created="2026-01-01T00:00:00",
        pool="frontier",
    )
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["explain", "frontier-rfr"], catch_exceptions=False)

    assert "pool" in result.output
    assert "frontier" in result.output
    assert "VERDICT: NOT READY" in result.output


def test_explain_substring_match_not_ambiguous(tmp_path: Path, monkeypatch) -> None:
    """Substring of another task's name must not select the wrong task."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "task-foo-bar", state="todo", created="2026-01-01T00:00:00")
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    # "task-foo" is a substring of "task-foo-bar" but is not a real task
    result = runner.invoke(cli, ["explain", "task-foo"])

    assert result.exit_code == 1
    assert "not found" in result.output.lower()
