"""Exit-code contract for `gptodo edit` refusals.

A refused edit must be distinguishable from a successful one by the exit code
alone, because autonomous callers chain `gptodo edit … && <next step>`. Before
this contract, unknown-field / invalid-value refusals printed a message and
returned, so the caller saw exit 0 and continued against an unmodified task.

Matrix (see knowledge/research/2026-11-08-task-authoring-interface-review-round3.md):

    input                                   exit    applied
    legal --set                             0       yes
    invalid value for a known field         1       no
    unknown field                           1       no
    terminal-state reopen                   1       no
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.utils import load_tasks


def _write(tasks_dir: Path, name: str = "t", state: str = "todo") -> None:
    (tasks_dir / f"{name}.md").write_text(
        f"---\nstate: {state}\ncreated: 2026-06-01T00:00:00+00:00\n---\n# {name}\n"
    )


@pytest.fixture()
def tasks_dir(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "tasks"
    d.mkdir()
    _write(d)
    monkeypatch.chdir(tmp_path)
    return d


def _state(tasks_dir: Path) -> str:
    return str(load_tasks(tasks_dir)[0].metadata["state"])


def test_legal_set_exits_zero_and_applies(tasks_dir: Path) -> None:
    result = CliRunner().invoke(cli, ["edit", "t", "--set", "state", "active"])

    assert result.exit_code == 0, result.output
    assert _state(tasks_dir) == "active"


def test_unknown_field_exits_nonzero_and_applies_nothing(tasks_dir: Path) -> None:
    result = CliRunner().invoke(cli, ["edit", "t", "--set", "bogus_field", "x"])

    assert result.exit_code == 1, result.output
    assert "Unknown field: bogus_field" in result.output
    assert _state(tasks_dir) == "todo"


def test_invalid_value_exits_nonzero_and_applies_nothing(tasks_dir: Path) -> None:
    result = CliRunner().invoke(cli, ["edit", "t", "--set", "priority", "urgent"])

    assert result.exit_code == 1, result.output
    assert "Invalid priority: urgent" in result.output
    assert _state(tasks_dir) == "todo"
    assert "priority" not in load_tasks(tasks_dir)[0].metadata


def test_list_field_via_set_exits_nonzero(tasks_dir: Path) -> None:
    result = CliRunner().invoke(cli, ["edit", "t", "--set", "tags", "feature"])

    assert result.exit_code == 1, result.output
    assert "is a list field" in result.output
    assert "tags" not in load_tasks(tasks_dir)[0].metadata


@pytest.mark.parametrize("op", ["--add", "--remove"])
def test_add_remove_unknown_field_exits_nonzero(tasks_dir: Path, op: str) -> None:
    result = CliRunner().invoke(cli, ["edit", "t", op, "bogus_field", "x"])

    assert result.exit_code == 1, result.output
    assert "Cannot" in result.output and "bogus_field" in result.output
    assert "bogus_field" not in load_tasks(tasks_dir)[0].metadata


@pytest.mark.parametrize(
    ("field", "value", "needle"),
    [
        ("wait", "not-a-date", "Invalid wait format"),
        ("waiting_since", "not-a-date", "Invalid waiting_since format"),
        ("created", "not-a-date", "Invalid created date format"),
        ("recur", "every other tuesday", "Invalid recur format"),
    ],
)
def test_invalid_format_refusals_exit_nonzero(
    tasks_dir: Path, field: str, value: str, needle: str
) -> None:
    before = (tasks_dir / "t.md").read_text()

    result = CliRunner().invoke(cli, ["edit", "t", "--set", field, value])

    assert result.exit_code == 1, result.output
    assert needle in result.output
    assert (tasks_dir / "t.md").read_text() == before


def test_terminal_reopen_exits_one_without_mutation(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "closed", "done")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["edit", "closed", "--set", "state", "todo"])

    assert result.exit_code == 1, result.output
    assert _state(tasks_dir) == "done"
