"""Regression tests for load_tasks error surfacing.

A task file with broken YAML frontmatter (e.g. an unquoted ``next_action: foo
\\`:1\\` bar`` that PyYAML mis-parses as a mapping) was previously dropped
silently — the file disappeared from the loaded list, the loader logged to
stderr, and ``gptodo check`` reported "All N tasks verified" with the wrong
N. The ``errors_out`` parameter lets callers detect this case.
"""

import subprocess
from pathlib import Path

from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.utils import load_tasks


VALID_TASK = """\
---
state: backlog
created: 2026-04-27T00:00:00+00:00
---
# Valid Task
"""

# Real reproducer from session 21b1 (2026-04-27): an unquoted next_action that
# embeds a backtick-colon-digit-backtick (`\`:1\``) makes PyYAML attempt a
# mapping parse inside the continuation and the entire frontmatter block fails.
BROKEN_TASK = """\
---
state: backlog
created: 2026-04-27T00:00:00+00:00
next_action: Run user-testing loop on `v0.31.1.dev202604277` (or later) on
  display `:1` — verify settings polish from #2247/#2248 in packaged build,
  then complete one real in-app message/tool roundtrip
---
# Broken Task
"""


def commit_task_fixtures(repo_root: Path, author: str = "Poison Author") -> None:
    """Commit task fixtures so warning attribution exercises real git history."""
    subprocess.run(["git", "init", "--quiet"], cwd=repo_root, check=True)
    subprocess.run(["git", "add", "tasks"], cwd=repo_root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            f"user.name={author}",
            "-c",
            "user.email=poison@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--no-verify",
            "--quiet",
            "-m",
            "test fixtures",
        ],
        cwd=repo_root,
        check=True,
    )


def test_load_tasks_appends_to_errors_out_when_yaml_invalid(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "valid.md").write_text(VALID_TASK)
    (tasks_dir / "broken.md").write_text(BROKEN_TASK)

    errors: list[tuple[Path, str]] = []
    tasks = load_tasks(tasks_dir, errors_out=errors)

    assert len(tasks) == 1, "broken file should not appear in the loaded list"
    assert tasks[0].name == "valid"
    assert len(errors) == 1, f"broken file should be reported in errors_out, got: {errors}"
    file, msg = errors[0]
    assert file.name == "broken.md"
    assert msg, "error message must not be empty"


def test_load_tasks_default_behavior_unchanged(tmp_path: Path) -> None:
    """Without errors_out, behavior matches the legacy contract: silent drop."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "valid.md").write_text(VALID_TASK)
    (tasks_dir / "broken.md").write_text(BROKEN_TASK)

    tasks = load_tasks(tasks_dir)

    assert len(tasks) == 1
    assert tasks[0].name == "valid"


def test_check_command_surfaces_unloadable_files(tmp_path: Path, monkeypatch) -> None:
    """`gptodo check` must fail loud — not report success — when a task file
    is unloadable. Previously the success line read 'All N tasks verified'
    with N silently low."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "valid.md").write_text(VALID_TASK)
    (tasks_dir / "broken.md").write_text(BROKEN_TASK)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["check"])

    assert result.exit_code == 1, (
        f"check must exit non-zero when a file is unloadable, "
        f"got {result.exit_code}\noutput: {result.output}"
    )
    assert "Unloadable Task Files" in result.output
    assert "broken.md" in result.output
    # Must NOT claim success
    assert "tasks verified successfully" not in result.output


def test_check_targeted_file_surfaces_unloadable(tmp_path: Path, monkeypatch) -> None:
    """`gptodo check tasks/broken.md` must also fail loud — the early-return
    path in the task_files branch must not suppress load_errors (P1 gap)."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "valid.md").write_text(VALID_TASK)
    (tasks_dir / "broken.md").write_text(BROKEN_TASK)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["check", "tasks/broken.md"])

    assert result.exit_code == 1, (
        f"check tasks/broken.md must exit non-zero when the file is unloadable, "
        f"got {result.exit_code}\noutput: {result.output}"
    )
    assert "Unloadable Task Files" in result.output
    assert "broken.md" in result.output


def test_check_targeted_file_warns_on_out_of_scope_unloadable(tmp_path: Path, monkeypatch) -> None:
    """A broken sibling must not poison a scoped check of a valid task."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "valid.md").write_text(VALID_TASK)
    (tasks_dir / "broken.md").write_text(BROKEN_TASK)
    commit_task_fixtures(tmp_path)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["check", "tasks/valid.md"])

    assert result.exit_code == 0, result.output
    assert "Out-of-scope unloadable task files" in result.output
    assert "tasks/broken.md" in result.output
    assert "last committed author: Poison Author" in result.output
    assert "All 1 tasks verified successfully" in result.output


def test_out_of_scope_unloadable_dependency_does_not_poison_target(
    tmp_path: Path, monkeypatch
) -> None:
    """A known broken dependency exists even though its metadata cannot load."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    dependent = VALID_TASK.replace("---\n# Valid Task", "requires: [broken]\n---\n# Valid Task")
    (tasks_dir / "valid.md").write_text(dependent)
    (tasks_dir / "broken.md").write_text(BROKEN_TASK)
    commit_task_fixtures(tmp_path)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["check", "tasks/valid.md"])

    assert result.exit_code == 0, result.output
    assert "Out-of-scope unloadable task files" in result.output
    assert "Dependency 'broken' not found" not in result.output
