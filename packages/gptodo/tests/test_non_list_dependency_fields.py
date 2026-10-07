"""A scalar in a list-typed frontmatter field must not crash the whole workspace.

``validate_task_file`` already reports ``Dependencies must be a list``, but the
loader still stored the raw scalar, so ``depends: 7`` made ``gptodo list``,
``check``, ``ready`` and ``next`` die with ``TypeError: 'int' object is not
iterable`` for every task in the directory, not just the malformed one.
"""

import pytest
from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.utils import load_tasks

GOOD = "---\nstate: active\ncreated: 2026-04-27T00:00:00+00:00\n---\n# Good\n"


def _write(tmp_path, name, frontmatter):
    tasks = tmp_path / "tasks"
    tasks.mkdir(exist_ok=True)
    (tasks / f"{name}.md").write_text(
        f"---\nstate: active\ncreated: 2026-04-27\n{frontmatter}---\nbody\n"
    )
    (tasks / "good.md").write_text(GOOD)
    return tasks


@pytest.mark.parametrize("field", ["depends", "requires", "tags", "related"])
@pytest.mark.parametrize("value", ["7", "foo", "null"])
def test_scalar_list_field_is_coerced_to_list(tmp_path, field, value):
    tasks = _write(tmp_path, "bad", f"{field}: {value}\n")
    loaded = {t.name: t for t in load_tasks(tasks)}
    bad = loaded["bad"]
    assert isinstance(getattr(bad, field), list)
    assert any("must be a list" in issue for issue in bad.issues)


@pytest.mark.parametrize("value", ["7", "foo", "null"])
def test_scalar_blocks_field_is_validated_without_crashing(tmp_path, value):
    tasks = _write(tmp_path, "bad", f"blocks: {value}\n")
    bad = {task.name: task for task in load_tasks(tasks)}["bad"]
    assert any("Blocks must be a list" in issue for issue in bad.issues)
    # Also verify the workspace is fully functional: commands must load both tasks
    result = CliRunner().invoke(cli, ["--tasks-dir", str(tasks), "list"])
    assert result.exception is None or isinstance(result.exception, SystemExit), result.output
    assert result.exit_code == 0, f"exit {result.exit_code}: {result.output}"
    assert "good" in result.output, result.output


@pytest.mark.parametrize(
    "command,expected_exit,expected_in_output",
    [
        ("list", 0, "bad"),  # workspace loaded; both tasks visible
        ("check", 1, "must be a list"),  # validation ran; errors reported
        ("ready", 0, "bad"),  # scalar dep coerced to [] so bad task is not blocked
        ("next", 0, "Task Metadata"),  # workspace functional; a task is surfaced
    ],
)
def test_commands_survive_scalar_depends(tmp_path, command, expected_exit, expected_in_output):
    """A scalar depends field must be coerced to [] so the task is NOT permanently blocked.

    ``ready`` asserts that ``bad`` itself is surfaced (not stuck behind a phantom dep).
    ``next`` picks whichever task wins — we only assert the workspace is functional.
    """
    tasks = _write(tmp_path, "bad", "depends: 7\ntags: foo\n")
    result = CliRunner().invoke(cli, ["--tasks-dir", str(tasks), command])
    assert (
        result.exception is None or isinstance(result.exception, SystemExit)
    ), f"Unexpected exception {type(result.exception).__name__}: {result.exception}\n{result.output}"
    assert result.exit_code == expected_exit, f"exit {result.exit_code}: {result.output}"
    assert expected_in_output in result.output, result.output


@pytest.mark.parametrize("value", ['""', '"   "', "''", "~"])
def test_empty_string_depends_does_not_create_phantom_dependency(tmp_path, value):
    """``depends: ""`` must coerce to [] so the task isn't blocked on a phantom dep named ""."""
    tasks = _write(tmp_path, "bad", f"depends: {value}\n")
    loaded = {t.name: t for t in load_tasks(tasks)}
    bad = loaded["bad"]
    assert bad.depends == [], f"expected [], got {bad.depends!r}"
    # The task must not appear blocked — ready should surface it
    result = CliRunner().invoke(cli, ["--tasks-dir", str(tasks), "ready"])
    assert result.exit_code == 0, f"exit {result.exit_code}: {result.output}"
    assert "bad" in result.output, result.output
