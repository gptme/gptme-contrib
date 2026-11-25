"""Parking a task into waiting asks "can you do this yourself?" on stderr."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from gptodo.cli import cli

ACTIVE_TASK = """\
---
state: active
created: 2026-06-16T00:00:00+00:00
---
# Some Active Task
"""

WAITING_TASK = """\
---
state: waiting
created: 2026-06-01T00:00:00+00:00
waiting_for: some-dependency
waiting_since: 2026-06-01T00:00:00+00:00
---
# Already Waiting Task
"""


def _setup(tmp_path: Path, monkeypatch, name: str, text: str) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(exist_ok=True)
    (tasks_dir / f"{name}.md").write_text(text)
    monkeypatch.chdir(tmp_path)


def test_edit_into_waiting_prints_self_check(tmp_path: Path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch, "my-task", ACTIVE_TASK)
    result = CliRunner().invoke(
        cli, ["edit", "my-task", "--set", "state", "waiting", "--set", "waiting_for", "x"]
    )
    assert result.exit_code == 0, result.output
    assert "Can you do this yourself?" in result.stderr
    assert "Can you do this yourself?" not in result.stdout


def test_edit_already_waiting_does_not_repeat(tmp_path: Path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch, "w-task", WAITING_TASK)
    result = CliRunner().invoke(cli, ["edit", "w-task", "--set", "waiting_for", "other"])
    assert result.exit_code == 0, result.output
    assert "Can you do this yourself?" not in result.output


def test_edit_other_state_does_not_print(tmp_path: Path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch, "my-task", ACTIVE_TASK)
    result = CliRunner().invoke(cli, ["edit", "my-task", "--set", "state", "ready_for_review"])
    assert result.exit_code == 0, result.output
    assert "Can you do this yourself?" not in result.output


def test_edit_json_mode_stays_quiet(tmp_path: Path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch, "my-task", ACTIVE_TASK)
    result = CliRunner().invoke(
        cli,
        ["edit", "my-task", "--json", "--set", "state", "waiting", "--set", "waiting_for", "x"],
    )
    assert result.exit_code == 0, result.output
    assert "Can you do this yourself?" not in result.output


def test_add_waiting_prints_self_check_on_stderr(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tasks").mkdir()
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli, ["add", "Blocked thing", "--state", "waiting", "--waiting-for", "x"], input=""
    )
    assert result.exit_code == 0, result.output
    assert "Can you do this yourself?" in result.stderr
    assert "Can you do this yourself?" not in result.stdout
