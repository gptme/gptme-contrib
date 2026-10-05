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


@pytest.mark.parametrize("field", ["depends", "requires", "tags", "related", "blocks"])
@pytest.mark.parametrize("value", ["7", "foo", "null"])
def test_scalar_list_field_is_coerced_to_list(tmp_path, field, value):
    tasks = _write(tmp_path, "bad", f"{field}: {value}\n")
    loaded = {t.name: t for t in load_tasks(tasks)}
    bad = loaded["bad"]
    for attr in ("depends", "requires", "tags", "related"):
        assert isinstance(getattr(bad, attr), list), attr
    assert any("must be a list" in i for i in bad.issues)


@pytest.mark.parametrize("command", ["list", "check", "ready", "next"])
def test_commands_survive_scalar_depends(tmp_path, command):
    tasks = _write(tmp_path, "bad", "depends: 7\ntags: foo\n")
    result = CliRunner().invoke(cli, ["--tasks-dir", str(tasks), command])
    assert not isinstance(result.exception, TypeError), result.output
