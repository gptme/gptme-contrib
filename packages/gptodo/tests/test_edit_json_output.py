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
    monkeypatch.setenv("GPTODO_TASKS_DIR", str(d))
    monkeypatch.setenv("TASKS_REPO_ROOT", str(tmp_path))
    return d


def _edit_json(tasks_dir: Path, *args: str) -> tuple[dict, int]:
    """Parse all of stdout, so stray human output cannot satisfy the contract."""
    result = CliRunner().invoke(cli, ["edit", "--json", *args])
    return json.loads(result.stdout), result.exit_code


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
    assert t["changes"]["state"] == {"from": "todo", "to": "active"}
    assert t["changes"]["priority"] == {"from": None, "to": "high"}


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


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--set", "priority", "urgent"), "Invalid priority"),
        ((), "No changes specified"),
        (("--set", "unknown", "value"), "Unknown field"),
        (("--set", "tags", "tag"), "is a list field"),
        (("--set", "wait", "invalid"), "Invalid wait format"),
        (("--set", "waiting_since", "invalid"), "Invalid waiting_since format"),
        (("--set", "created", "invalid"), "Invalid created date format"),
        (("--set", "recur", "invalid"), "Invalid recur format"),
        (("--add", "priority", "high"), "Cannot add to field"),
        (("--remove", "priority", "high"), "Cannot remove to field"),
        (("--set-subtask", "step", "invalid"), "Invalid subtask state"),
        (("--set-subtask", " ", "done"), "Subtask selector must not be empty"),
        (("--set-subtask", "absent", "done"), "absent"),
    ],
)
def test_json_validation_refusals(tasks_dir: Path, args: tuple[str, ...], message: str) -> None:
    before = (tasks_dir / "t.md").read_text()
    payload, code = _edit_json(tasks_dir, "t", *args)
    assert code == payload["exit"] == 1
    assert payload["id"] == "t"
    assert message in payload["error"]
    assert (tasks_dir / "t.md").read_text() == before


def test_json_pending_checklist_refusal(tasks_dir: Path) -> None:
    with (tasks_dir / "t.md").open("a") as f:
        f.write("\n- [ ] unfinished step\n")
    payload, code = _edit_json(tasks_dir, "t", "--set", "state", "done")
    assert code == payload["exit"] == 1
    assert "Cannot mark done" in payload["error"]


def test_json_empty_tasks_with_subtask_refusal(tasks_dir: Path) -> None:
    (tasks_dir / "t.md").unlink()
    payload, code = _edit_json(tasks_dir, "t", "--set-subtask", "step", "done")
    assert code == payload["exit"] == 1
    assert payload["error"] == "No tasks found"


def test_json_strict_transition_refusal(tasks_dir: Path, monkeypatch) -> None:
    _write(tasks_dir, state="active")
    monkeypatch.setenv("GPTODO_STRICT_TRANSITIONS", "1")
    payload, code = _edit_json(tasks_dir, "t", "--set", "state", "todo")
    assert code == payload["exit"] == 1
    assert "illegal transition" in payload["error"]


def test_json_replacing_yaml_date(tasks_dir: Path) -> None:
    (tasks_dir / "t.md").write_text("---\nstate: todo\nwait: 2026-06-01\n---\n# t\n")
    payload, code = _edit_json(tasks_dir, "t", "--set", "wait", "2026-06-02")
    assert code == 0
    assert payload["tasks"][0]["changes"]["wait"] == {"from": "2026-06-01", "to": "2026-06-02"}


def test_json_invalid_extension_refusal(tasks_dir: Path, monkeypatch) -> None:
    monkeypatch.setenv("GPTODO_EXTRA_FRONTMATTER_FIELDS", "custom_field")
    payload, code = _edit_json(tasks_dir, "t", "--set", "custom_field", "{invalid")
    assert code == payload["exit"] == 1
    assert "could not be parsed" in payload["error"]


def test_click_usage_errors_are_not_json(tasks_dir: Path) -> None:
    result = CliRunner().invoke(cli, ["edit", "--json", "t", "--unknown"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "No such option" in result.stderr


@pytest.mark.parametrize("state,strict", [("done", False), ("active", True)])
def test_json_batch_identifies_transition_refusal(
    tasks_dir: Path, monkeypatch, state, strict
) -> None:
    _write(tasks_dir, "blocked", state=state)
    if strict:
        monkeypatch.setenv("GPTODO_STRICT_TRANSITIONS", "1")
    payload, code = _edit_json(tasks_dir, "t", "blocked", "--set", "state", "todo")
    assert code == payload["exit"] == 1
    assert payload["id"] == "blocked"


def test_json_batch_identifies_pending_task(tasks_dir: Path) -> None:
    _write(tasks_dir, "blocked")
    with (tasks_dir / "blocked.md").open("a") as f:
        f.write("\n- [ ] unfinished step\n")
    payload, code = _edit_json(tasks_dir, "t", "blocked", "--set", "state", "done")
    assert code == payload["exit"] == 1
    assert payload["id"] == "blocked"
