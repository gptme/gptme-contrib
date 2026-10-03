"""Dependency-only archive loads must avoid unused body scans, not parsing."""

from pathlib import Path

from click.testing import CliRunner

from gptodo.cli import cli
from gptodo import utils


def test_dependency_only_load_preserves_metadata_errors_and_default_counts(tmp_path, monkeypatch):
    (tmp_path / "valid.md").write_text(
        "---\nstate: done\ncreated: 2026-01-01\nrequires: [other]\n---\n- [x] Done\n- [ ] Pending\n"
    )
    (tmp_path / "broken.md").write_text("---\nstate: [\n---\n")
    default = utils.load_tasks(tmp_path)
    assert default[0].subtasks.completed == 1
    assert default[0].subtasks.total == 2

    def forbidden_count(content):
        raise AssertionError("dependency-only load scanned checkboxes")

    monkeypatch.setattr(utils, "count_subtasks", forbidden_count)
    errors = []
    tasks = utils.load_tasks(tmp_path, errors_out=errors, include_subtasks=False)
    assert len(tasks) == 1
    assert tasks[0].requires == ["other"]
    assert tasks[0].metadata == default[0].metadata
    assert tasks[0].subtasks.total == 0
    assert [path.name for path, _ in errors] == ["broken.md"]


def test_check_archive_dependency_universe_does_not_scan_body(tmp_path: Path, monkeypatch):
    tasks = tmp_path / "tasks"
    archive = tasks / "archive"
    archive.mkdir(parents=True)
    (tasks / "live.md").write_text(
        "---\nstate: backlog\ncreated: 2026-01-01\nrequires: [old]\n---\n# Live\n"
    )
    (archive / "old.md").write_text(
        "---\nstate: done\ncreated: 2026-01-01\n---\nARCHIVE_BODY\n- [x] Done\n"
    )
    from gptodo import cli as cli_module

    original = cli_module.load_tasks
    archive_calls = []

    def guarded_load(directory, *args, **kwargs):
        if directory == archive:
            archive_calls.append(kwargs.get("include_subtasks", True))
        return original(directory, *args, **kwargs)

    monkeypatch.setattr(cli_module, "load_tasks", guarded_load)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["check", "tasks/live.md"])
    assert result.exit_code == 0, result.output
    assert "All 1 tasks verified successfully" in result.output
    assert archive_calls == [False]


def test_dependency_only_archive_still_rejects_cycle(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    archive = tasks / "archive"
    archive.mkdir(parents=True)
    (tasks / "live.md").write_text(
        "---\nstate: backlog\ncreated: 2026-01-01\nrequires: [old]\n---\n# Live\n"
    )
    (archive / "old.md").write_text(
        "---\nstate: done\ncreated: 2026-01-01\nrequires: [live]\n---\n- [x] Done\n"
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["check", "tasks/live.md"])
    assert result.exit_code != 0
    assert "Circular dependency" in result.output


def test_scoped_archived_target_keeps_full_counts(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    archive = tasks / "archive"
    archive.mkdir(parents=True)
    (tasks / "live.md").write_text("---\nstate: backlog\ncreated: 2026-01-01\n---\n# Live\n")
    (archive / "old.md").write_text("---\nstate: done\ncreated: 2026-01-01\n---\n- [x] Done\n")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["check", "tasks/archive/old.md"])
    assert result.exit_code == 0, result.output
    assert "All 1 tasks verified successfully! (1 with subtasks)" in result.output


def test_missing_dependency_still_fails(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    (tasks / "archive").mkdir(parents=True)
    (tasks / "live.md").write_text(
        "---\nstate: backlog\ncreated: 2026-01-01\nrequires: [missing]\n---\n# Live\n"
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["check", "tasks/live.md"])
    assert result.exit_code != 0
    assert "Dependency 'missing' not found" in result.output
