"""Regression test: `gptodo check` must not report a false "not found"
dependency for tasks archived under tasks/archive/. The default
`load_tasks(tasks_dir)` call uses a non-recursive glob, so it never sees
`tasks/archive/<id>.md`; any live task depending on an archived-but-done task
previously failed validation. See
tasks/gptodo-check-archived-dependency-false-positive.md for the incident.
"""

from pathlib import Path

from click.testing import CliRunner

from gptodo.cli import cli


DEPENDENT_TASK = """\
---
state: backlog
created: 2026-09-18T00:00:00+00:00
depends: [archived-dep]
---
# Dependent Task
"""

ARCHIVED_DEP_TASK = """\
---
state: done
created: 2026-09-01T00:00:00+00:00
---
# Archived Dependency
"""


def _write_fixtures(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    archive_dir = tasks_dir / "archive"
    archive_dir.mkdir()

    (tasks_dir / "foo.md").write_text(DEPENDENT_TASK)
    (archive_dir / "archived-dep.md").write_text(ARCHIVED_DEP_TASK)


def test_check_scoped_finds_archived_dependency(tmp_path: Path, monkeypatch) -> None:
    """A live task depending on an archived-but-done task must pass a scoped
    check — the path pre-commit's validate-tasks hook exercises."""
    _write_fixtures(tmp_path)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["check", "tasks/foo.md"])

    assert result.exit_code == 0, result.output
    assert "not found" not in result.output
    assert "All 1 tasks verified successfully" in result.output


def test_check_full_run_finds_archived_dependency(tmp_path: Path, monkeypatch) -> None:
    """Same fix, exercised via the full (no-args) check path."""
    _write_fixtures(tmp_path)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["check"])

    assert result.exit_code == 0, result.output
    assert "Dependency Issues" not in result.output


def test_check_full_run_does_not_validate_archived_tasks(tmp_path: Path, monkeypatch) -> None:
    """Archived tasks are resolved for dependency lookups, but not pulled
    into the full "check all" validation count — only tasks/*.md are."""
    _write_fixtures(tmp_path)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["check"])

    assert result.exit_code == 0, result.output
    assert "All 1 tasks verified successfully" in result.output
