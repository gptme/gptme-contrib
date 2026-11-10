"""Tests for `gptodo edit --json` machine-readable output.

Schema contract:
  success: {"tasks": [{"id": str, "changes": {field: {"from": any, "to": any}}}], "exit": 0}
  error:   {"id": str|None, "error": str, "exit": 1}
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from gptodo.cli import cli


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


def _edit_json(tasks_dir: Path, *args: str) -> tuple[dict, int]:
    """Invoke `gptodo edit --json <args>` and return (parsed_last_json_line, exit_code).

    CliRunner merges stdout+stderr into result.output, so we parse the last
    non-empty line, which is always the JSON emitted by --json mode.
    """
    result = CliRunner().invoke(cli, ["edit", "--json", *args])
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    return json.loads(lines[-1]), result.exit_code


def test_json_state_change_contains_task_id_and_diff(tasks_dir: Path) -> None:
    payload, code = _edit_json(tasks_dir, "t", "--set", "state", "active")
    assert code == 0
    assert payload["exit"] == 0
    assert len(payload["tasks"]) == 1
    t = payload["tasks"][0]
    assert t["id"] == "t"
    assert t["changes"]["state"] == {"from": "todo", "to": "active"}


def test_json_no_op_has_empty_changes(tasks_dir: Path) -> None:
    # Setting the same state produces no changes (mutate_task records no diff).
    payload, code = _edit_json(tasks_dir, "t", "--set", "state", "todo")
    assert code == 0
    assert payload["exit"] == 0
    t = payload["tasks"][0]
    assert t["changes"] == {}


def test_json_multiple_fields_all_appear(tasks_dir: Path) -> None:
    payload, code = _edit_json(
        tasks_dir, "t", "--set", "state", "active", "--set", "priority", "high"
    )
    assert code == 0
    t = payload["tasks"][0]
    assert "state" in t["changes"] or "priority" in t["changes"]


def test_json_missing_task_emits_error_object(tasks_dir: Path) -> None:
    payload, code = _edit_json(tasks_dir, "no-such-task", "--set", "state", "active")
    assert code != 0
    assert payload["exit"] == 1
    assert "error" in payload


def test_json_terminal_reopen_emits_error_on_stdout(tasks_dir: Path) -> None:
    _write(tasks_dir, "done-task", state="done")
    payload, code = _edit_json(tasks_dir, "done-task", "--set", "state", "active")
    assert code != 0
    assert payload["exit"] == 1
    assert "error" in payload


def test_json_last_line_is_valid_json(tasks_dir: Path) -> None:
    """The last output line must be valid JSON regardless of any preceding lines."""
    result = CliRunner().invoke(cli, ["edit", "--json", "t", "--set", "state", "active"])
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    parsed = json.loads(lines[-1])
    assert isinstance(parsed, dict)
    assert "exit" in parsed
