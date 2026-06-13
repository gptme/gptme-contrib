"""Tests for `gptodo status --json` machine-readable output.

The autonomous-run scripts must not grep rich-rendered human output to detect
active work — `--json` is the stable contract they parse instead.
"""

import json
from pathlib import Path

from click.testing import CliRunner

from gptodo.cli import cli


def write_task(tasks_dir: Path, name: str, **metadata: object) -> None:
    """Write a minimal task file with YAML frontmatter."""
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


def _run_status_json(tmp_path: Path, monkeypatch, *extra_args: str) -> dict:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["status", "--json", *extra_args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_status_json_emits_valid_parseable_json(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "a", state="active", created="2026-03-28T00:00:00")
    write_task(tasks_dir, "b", state="backlog", created="2026-03-28T00:00:00")

    payload = _run_status_json(tmp_path, monkeypatch)

    assert payload["type"] == "tasks"
    assert {t["id"] for t in payload["tasks"]} == {"a", "b"}
    assert payload["summary"]["total"] == 2


def test_status_json_summary_counts_by_state(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "a", state="active", created="2026-03-28T00:00:00")
    write_task(tasks_dir, "b", state="backlog", created="2026-03-28T00:00:00")
    write_task(tasks_dir, "c", state="backlog", created="2026-03-28T00:00:00")
    write_task(tasks_dir, "d", state="done", created="2026-03-28T00:00:00")

    payload = _run_status_json(tmp_path, monkeypatch)

    by_state = payload["summary"]["by_state"]
    assert by_state == {"active": 1, "backlog": 2, "done": 1}
    # total == flattened task count (no double counting across buckets)
    assert payload["summary"]["total"] == len(payload["tasks"]) == 4


def test_status_json_active_detection_contract(tmp_path: Path, monkeypatch) -> None:
    """The contract autonomous-run-cc.sh relies on: detect any active task."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "only-backlog", state="backlog", created="2026-03-28T00:00:00")

    payload = _run_status_json(tmp_path, monkeypatch)
    has_active = any(t["state"] == "active" for t in payload["tasks"])
    assert has_active is False

    write_task(tasks_dir, "now-active", state="active", created="2026-03-28T00:00:00")
    payload = _run_status_json(tmp_path, monkeypatch)
    has_active = any(t["state"] == "active" for t in payload["tasks"])
    assert has_active is True


def test_status_json_empty_directory(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tasks").mkdir()

    payload = _run_status_json(tmp_path, monkeypatch)

    assert payload["tasks"] == []
    assert payload["summary"]["total"] == 0
    assert payload["summary"]["by_state"] == {}


def test_status_json_all_groups_by_type(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "a", state="active", created="2026-03-28T00:00:00")

    payload = _run_status_json(tmp_path, monkeypatch, "--all")

    assert payload["all"] is True
    assert "tasks" in payload["types"]
    assert payload["types"]["tasks"]["summary"]["total"] == 1


def test_status_json_includes_serialized_task_fields(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(
        tasks_dir,
        "rich-task",
        state="active",
        priority="high",
        created="2026-03-28T00:00:00",
        tags=["infra", "tooling"],
    )

    payload = _run_status_json(tmp_path, monkeypatch)
    task = next(t for t in payload["tasks"] if t["id"] == "rich-task")
    assert task["state"] == "active"
    assert task["priority"] == "high"
    assert task["tags"] == ["infra", "tooling"]
    assert task["subtasks"] == {"completed": 0, "total": 0}
