"""Repeated checklist edits validate the whole batch before writing any task."""

import pytest
from click.testing import CliRunner

from gptodo.cli import cli


def make_tasks(tmp_path, monkeypatch, bodies):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    paths = []
    for name, body in bodies.items():
        path = tasks_dir / f"{name}.md"
        path.write_text(
            f"---\nstate: active\ncreated: 2026-01-01T00:00:00+00:00\n---\n# {name}\n\n{body}\n"
        )
        paths.append(path)
    return paths


def invoke(ids, edits):
    args = ["edit", *ids]
    for text, state in edits:
        args.extend(["--set-subtask", text, state])
    return CliRunner().invoke(cli, args, catch_exceptions=False)


def test_repeated_edits_apply_all_directions_to_all_tasks(tmp_path, monkeypatch):
    paths = make_tasks(
        tmp_path,
        monkeypatch,
        {
            name: "- [ ] alpha\n  - [x] beta\n> - [-] gamma (deferred: see (issue #5))"
            for name in ("first", "second")
        },
    )
    result = invoke(["first", "second"], [("alpha", "done"), ("beta", "todo"), ("gamma", "done")])
    assert result.exit_code == 0, result.output
    for path in paths:
        content = path.read_text()
        assert "- [x] alpha" in content
        assert "  - [ ] beta" in content
        assert "> - [x] gamma" in content
        assert "deferred:" not in content


@pytest.mark.parametrize(
    "edits",
    [
        [("alpha", "done"), ("missing", "done")],
        [("alpha", "done"), ("beta", "done")],
        [("alpha", "done"), ("alpha", "todo")],
        [("alpha", "done"), ("alp", "done")],
        [("alpha", "done"), ("beta one", "invalid")],
        [("alpha", "done"), ("", "done")],
    ],
)
def test_rejected_batch_preserves_every_file(tmp_path, monkeypatch, edits):
    paths = make_tasks(
        tmp_path,
        monkeypatch,
        {
            "first": "- [ ] alpha\n- [ ] beta one\n- [ ] beta two",
            "second": "- [ ] alpha\n- [ ] beta one\n- [ ] beta two",
        },
    )
    before = [path.read_bytes() for path in paths]
    result = invoke(["first", "second"], edits)
    assert result.exit_code != 0, result.output
    assert [path.read_bytes() for path in paths] == before


def test_later_task_missing_selector_does_not_write_first(tmp_path, monkeypatch):
    paths = make_tasks(
        tmp_path,
        monkeypatch,
        {
            "first": "- [ ] alpha\n- [ ] beta",
            "second": "- [ ] alpha",
        },
    )
    before = [path.read_bytes() for path in paths]
    result = invoke(["first", "second"], [("alpha", "done"), ("beta", "done")])
    assert result.exit_code != 0, result.output
    assert [path.read_bytes() for path in paths] == before


def test_prose_is_not_a_checkbox_match(tmp_path, monkeypatch):
    paths = make_tasks(
        tmp_path,
        monkeypatch,
        {
            "first": "Prose about alpha - [ ]\n- [ ] alpha\n- [x] Refer to - [ ] old checklist",
        },
    )
    result = invoke(["first"], [("alpha", "done"), ("Refer to - [ ] old checklist", "todo")])
    assert result.exit_code == 0, result.output
    content = paths[0].read_text()
    assert "Prose about alpha - [ ]" in content
    assert "- [x] alpha" in content
    assert "- [ ] Refer to - [ ] old checklist" in content


def test_missing_task_rejects_batch(tmp_path, monkeypatch):
    paths = make_tasks(tmp_path, monkeypatch, {"first": "- [ ] alpha"})
    before = paths[0].read_bytes()
    result = invoke(["first", "absent"], [("alpha", "done")])
    assert result.exit_code != 0, result.output
    assert paths[0].read_bytes() == before


def test_later_ambiguity_preserves_metadata_and_bodies(tmp_path, monkeypatch):
    paths = make_tasks(
        tmp_path,
        monkeypatch,
        {
            "first": "- [ ] alpha\n- [ ] beta",
            "second": "- [ ] alpha\n- [ ] beta one\n- [ ] beta two",
        },
    )
    before = [path.read_bytes() for path in paths]
    result = CliRunner().invoke(
        cli,
        [
            "edit",
            "first",
            "second",
            "--set",
            "priority",
            "high",
            "--set-subtask",
            "alpha",
            "done",
            "--set-subtask",
            "beta",
            "done",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code != 0, result.output
    assert "Ambiguous subtask" in result.output
    assert [path.read_bytes() for path in paths] == before


def test_empty_workspace_rejects_subtask_edit(tmp_path, monkeypatch):
    make_tasks(tmp_path, monkeypatch, {})
    result = invoke(["absent"], [("alpha", "done")])
    assert result.exit_code != 0, result.output
