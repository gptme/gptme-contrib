"""Tests for editing coordination fields via `gptodo edit --set`.

`tracking_issue` (human URL of the live coordination issue/PR) and
`upstream_coordination_id` (machine claim key, e.g. github:OWNER/REPO#NUM) are
first-class task frontmatter fields documented in TASKS.md and read by the
CASCADE selector for cross-repo claims. They must be settable via the CLI, not
only by hand-editing frontmatter.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.utils import load_tasks


TASK = """\
---
state: active
created: 2026-06-11T00:00:00+00:00
---
# Cross-Repo Task
"""


def test_edit_set_tracking_issue(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "cross-repo.md").write_text(TASK)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "edit",
            "cross-repo",
            "--set",
            "tracking_issue",
            "https://github.com/gptme/gptme/pull/2837",
        ],
    )

    assert result.exit_code == 0, f"edit failed: {result.output}"

    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    assert tasks[0].metadata["tracking_issue"] == "https://github.com/gptme/gptme/pull/2837"


def test_edit_set_upstream_coordination_id(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "cross-repo.md").write_text(TASK)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "edit",
            "cross-repo",
            "--set",
            "upstream_coordination_id",
            "github:gptme/gptme#2837",
        ],
    )

    assert result.exit_code == 0, f"edit failed: {result.output}"

    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    assert tasks[0].metadata["upstream_coordination_id"] == "github:gptme/gptme#2837"


def test_edit_clear_tracking_issue(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_with = TASK.replace(
        "created: 2026-06-11T00:00:00+00:00\n",
        "created: 2026-06-11T00:00:00+00:00\ntracking_issue: https://github.com/gptme/gptme/pull/2837\n",
    )
    (tasks_dir / "cross-repo.md").write_text(task_with)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli,
        ["edit", "cross-repo", "--set", "tracking_issue", "none"],
    )

    assert result.exit_code == 0, f"edit failed: {result.output}"

    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    assert tasks[0].metadata.get("tracking_issue") in (None, "")


def test_edit_clear_upstream_coordination_id(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_with = TASK.replace(
        "created: 2026-06-11T00:00:00+00:00\n",
        "created: 2026-06-11T00:00:00+00:00\nupstream_coordination_id: github:gptme/gptme#2837\n",
    )
    (tasks_dir / "cross-repo.md").write_text(task_with)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli,
        ["edit", "cross-repo", "--set", "upstream_coordination_id", "none"],
    )

    assert result.exit_code == 0, f"edit failed: {result.output}"

    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    assert tasks[0].metadata.get("upstream_coordination_id") in (None, "")
