"""Regression tests: `gptodo ready` / `gptodo next` must not falsely block tasks
whose dependencies have been archived under tasks/archive/.

Before the fix, `ready` and `next` built their dependency-lookup dict from
`load_tasks(tasks_dir)` (non-recursive — misses archive/), so any task whose
prerequisite was archived was permanently stuck in "missing dependency = blocked".
`gptodo check` already resolved this correctly via `dependency_universe`; this
verifies the same fix landed in `ready` and `next`.
"""

import json
from pathlib import Path

from click.testing import CliRunner

from gptodo.cli import cli


DEPENDENT_TASK = """\
---
state: backlog
created: 2026-09-25T00:00:00+00:00
depends: [archived-prereq]
---
# Dependent task (depends on an archived prerequisite)
"""

ARCHIVED_DONE_TASK = """\
---
state: done
created: 2026-09-01T00:00:00+00:00
---
# Archived prerequisite (done, now in archive)
"""

ARCHIVED_ACTIVE_TASK = """\
---
state: active
created: 2026-09-01T00:00:00+00:00
---
# Archived prerequisite (still active — must still block)
"""


def _write_fixtures(tmp_path: Path, archived_content: str = ARCHIVED_DONE_TASK) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    archive_dir = tasks_dir / "archive"
    archive_dir.mkdir()

    (tasks_dir / "foo.md").write_text(DEPENDENT_TASK)
    (archive_dir / "archived-prereq.md").write_text(archived_content)


def test_ready_treats_archived_done_dep_as_met(tmp_path: Path, monkeypatch) -> None:
    """`gptodo ready` must surface a task whose only dependency is an archived done task."""
    _write_fixtures(tmp_path, ARCHIVED_DONE_TASK)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["ready", "--state", "backlog", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    ready_names = [t["name"] for t in data["ready_tasks"]]
    assert "foo" in ready_names, (
        "Task depending on an archived-done prerequisite must be ready, got: "
        f"{ready_names!r}\nFull output:\n{result.output}"
    )


def test_ready_keeps_archived_non_terminal_dep_blocking(tmp_path: Path, monkeypatch) -> None:
    """`gptodo ready` must NOT surface a task whose dependency is archived but still active."""
    _write_fixtures(tmp_path, ARCHIVED_ACTIVE_TASK)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["ready", "--state", "backlog", "--json"])

    assert result.exit_code == 0, result.output
    # When no tasks are ready, the CLI emits both a JSON payload and an error message.
    # Parse from the first '{' to handle any leading stderr text mixed in.
    start = result.output.find("{")
    end = result.output.rfind("}") + 1
    data = json.loads(result.output[start:end]) if start >= 0 else {"ready_tasks": []}
    ready_names = [t["name"] for t in data.get("ready_tasks", [])]
    assert "foo" not in ready_names, (
        "Task depending on an archived-but-still-active prerequisite must remain blocked, "
        f"got ready: {ready_names!r}"
    )


def test_next_treats_archived_done_dep_as_met(tmp_path: Path, monkeypatch) -> None:
    """`gptodo next` must return the task when its dependency is archived and done."""
    _write_fixtures(tmp_path, ARCHIVED_DONE_TASK)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["next", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data.get("next_task") is not None, (
        "Expected a next task but got None.\nFull output:\n" + result.output
    )
    assert data["next_task"]["name"] == "foo"
