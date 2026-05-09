"""Tests for the success_criterion field in task frontmatter.

success_criterion is an optional string that expresses a verifiable "done" gate
so agents can self-check before marking a task complete (Outcomes-style).
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.utils import load_tasks


TASK_WITH_CRITERION = """\
---
state: active
created: 2026-05-09T00:00:00+00:00
success_criterion: "All 191 gptodo tests pass and gptodo check shows no issues"
---
# Task With Success Criterion
"""

TASK_WITHOUT_CRITERION = """\
---
state: backlog
created: 2026-05-09T00:00:00+00:00
---
# Simple Task
"""


def test_load_task_with_success_criterion(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "with-criterion.md").write_text(TASK_WITH_CRITERION)

    tasks = load_tasks(tasks_dir)

    assert len(tasks) == 1
    assert (
        tasks[0].success_criterion == "All 191 gptodo tests pass and gptodo check shows no issues"
    )


def test_load_task_without_success_criterion_defaults_to_none(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "without-criterion.md").write_text(TASK_WITHOUT_CRITERION)

    tasks = load_tasks(tasks_dir)

    assert len(tasks) == 1
    assert tasks[0].success_criterion is None


def test_show_displays_success_criterion(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "with-criterion.md").write_text(TASK_WITH_CRITERION)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["show", "with-criterion"])

    assert result.exit_code == 0, f"show failed: {result.output}"
    assert "Success Criterion" in result.output
    assert "All 191 gptodo tests pass" in result.output


def test_show_omits_success_criterion_when_unset(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "without-criterion.md").write_text(TASK_WITHOUT_CRITERION)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["show", "without-criterion"])

    assert result.exit_code == 0, f"show failed: {result.output}"
    assert "Success Criterion" not in result.output


def test_edit_set_success_criterion(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_file = tasks_dir / "without-criterion.md"
    task_file.write_text(TASK_WITHOUT_CRITERION)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "edit",
            "without-criterion",
            "--set",
            "success_criterion",
            "CI green and review addressed",
        ],
    )

    assert result.exit_code == 0, f"edit failed: {result.output}"

    # Verify it persisted
    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    assert tasks[0].success_criterion == "CI green and review addressed"


def test_edit_clear_success_criterion(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_file = tasks_dir / "with-criterion.md"
    task_file.write_text(TASK_WITH_CRITERION)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli,
        ["edit", "with-criterion", "--set", "success_criterion", "none"],
    )

    assert result.exit_code == 0, f"edit failed: {result.output}"

    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    assert tasks[0].success_criterion is None
