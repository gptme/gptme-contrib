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

from click.testing import CliRunner, Result

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

COLLISION_DEPENDENT_TASK = """\
---
state: backlog
created: 2026-09-25T00:00:00+00:00
depends: [collide]
---
# Dependent task (depends on a name that exists both live and archived)
"""

LIVE_TODO_TASK = """\
---
state: todo
created: 2026-09-20T00:00:00+00:00
---
# Live prerequisite (still todo — must block)
"""

ARCHIVED_SAME_NAME_DONE_TASK = """\
---
state: done
created: 2026-09-01T00:00:00+00:00
completed: 2026-09-02T00:00:00+00:00
---
# Archived prerequisite sharing a name with a live task
"""


def _parse_json(result: Result) -> dict:
    """Extract the JSON document from CLI output.

    `ready`/`next` print the JSON payload on stdout and any "No ready tasks
    found" notice on stderr. CliRunner hands back either the combined stream or
    stdout alone depending on the click version, so locate the first JSON value
    with ``raw_decode``. Raises if no JSON payload is present — a missing
    payload must fail the test, not silently degrade to an empty result.
    """
    raw = result.output
    start = raw.find("{")
    assert start >= 0, f"no JSON payload in CLI output: {raw!r}"
    data, _ = json.JSONDecoder().raw_decode(raw[start:])
    return data


def _write_fixtures(tmp_path: Path, archived_content: str = ARCHIVED_DONE_TASK) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    archive_dir = tasks_dir / "archive"
    archive_dir.mkdir()

    (tasks_dir / "foo.md").write_text(DEPENDENT_TASK)
    (archive_dir / "archived-prereq.md").write_text(archived_content)


def _write_collision_fixtures(tmp_path: Path) -> None:
    """A live `collide` task and an archived `collide` task coexist."""
    tasks_dir = tmp_path / "tasks"
    (tasks_dir / "archive").mkdir(parents=True)
    (tasks_dir / "foo.md").write_text(COLLISION_DEPENDENT_TASK)
    (tasks_dir / "collide.md").write_text(LIVE_TODO_TASK)
    (tasks_dir / "archive" / "collide.md").write_text(ARCHIVED_SAME_NAME_DONE_TASK)


def test_ready_treats_archived_done_dep_as_met(tmp_path: Path, monkeypatch) -> None:
    """`gptodo ready` must surface a task whose only dependency is an archived done task."""
    _write_fixtures(tmp_path, ARCHIVED_DONE_TASK)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["ready", "--state", "backlog", "--json"])

    assert result.exit_code == 0, result.output
    data = _parse_json(result)
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
    # Assert the payload shape itself (not merely "foo is absent"), so a malformed
    # or missing JSON payload fails instead of passing vacuously.
    data = _parse_json(result)
    assert data["ready_tasks"] == [], (
        "Task depending on an archived-but-still-active prerequisite must remain blocked, "
        f"got ready: {data['ready_tasks']!r}"
    )


def test_ready_live_dep_wins_over_same_named_archived_dep(tmp_path: Path, monkeypatch) -> None:
    """A live task must shadow a same-named archived task when resolving dependencies.

    Regression: merging archived tasks into the lookup dict keyed by name let a stale
    archived copy overwrite the live task, so a dependency that is still ``todo`` live
    but ``done`` in the archive falsely unblocked its dependents.
    """
    _write_collision_fixtures(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["ready", "--state", "backlog", "--json"])

    assert result.exit_code == 0, result.output
    data = _parse_json(result)
    assert data["ready_tasks"] == [], (
        "Live `collide` is still todo, so `foo` must remain blocked even though an "
        f"archived `collide` is done; got ready: {data['ready_tasks']!r}"
    )


def test_next_treats_archived_done_dep_as_met(tmp_path: Path, monkeypatch) -> None:
    """`gptodo next` must return the task when its dependency is archived and done."""
    _write_fixtures(tmp_path, ARCHIVED_DONE_TASK)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["next", "--json"])

    assert result.exit_code == 0, result.output
    data = _parse_json(result)
    assert data.get("next_task") is not None, (
        "Expected a next task but got None.\nFull output:\n" + result.output
    )
    assert data["next_task"]["name"] == "foo"
