"""Completion validates the projected checklist before any batch writes."""

import pytest
from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.frontmatter_compat import frontmatter


def make_tasks(tmp_path, monkeypatch, bodies, state="active", extra=""):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HOOK_TASK_DONE", raising=False)
    paths = []
    for name, body in bodies.items():
        path = tasks_dir / f"{name}.md"
        path.write_text(
            f"---\nstate: {state}\ncreated: 2026-01-01T00:00:00+00:00\n"
            f"{extra}---\n# {name}\n\n{body}\n"
        )
        paths.append(path)
    return paths


@pytest.mark.parametrize("force", [False, True])
def test_pending_completion_preserves_entire_batch(tmp_path, monkeypatch, force):
    paths = make_tasks(
        tmp_path, monkeypatch, {"first": "- [x] complete", "second": "- [ ] unfinished"}
    )
    before = [path.read_bytes() for path in paths]
    args = ["edit", "first", "second", "--set", "state", "done"]
    if force:
        args.append("--force")
    result = CliRunner().invoke(cli, args)
    assert result.exit_code != 0, result.output
    assert "second" in result.output
    assert "follow-up" in result.output
    assert [path.read_bytes() for path in paths] == before


@pytest.mark.parametrize(
    "body",
    [
        "- [x] complete",
        "- [-] dropped (reason: replaced)",
        "- [ ] ~~dropped~~ (reason: replaced)",
        "- [x] ~~dropped~~ (reason: replaced)",
        "- [x] Refer to - [ ] example\nProse about - [ ] another example",
        "```markdown\n- [ ] example\n```\n- [x] complete",
        "~~~~markdown\n```\n- [ ] example\n~~~\n~~~~\n- [x] complete",
    ],
)
def test_resolved_checklist_allows_completion(tmp_path, monkeypatch, body):
    (path,) = make_tasks(tmp_path, monkeypatch, {"task": body})
    result = CliRunner().invoke(cli, ["edit", "task", "--set", "state", "done"])
    assert result.exit_code == 0, result.output
    assert frontmatter.load(path)["state"] == "done"


def test_completion_uses_same_edit_checklist_changes(tmp_path, monkeypatch):
    (path,) = make_tasks(tmp_path, monkeypatch, {"task": "- [ ] remaining"})
    result = CliRunner().invoke(
        cli,
        ["edit", "task", "--set", "state", "done", "--set-subtask", "remaining", "done"],
    )
    assert result.exit_code == 0, result.output
    post = frontmatter.load(path)
    assert post["state"] == "done"
    assert "- [x] remaining" in post.content


def test_recurring_completion_rejects_pending_before_reset(tmp_path, monkeypatch):
    (path,) = make_tasks(tmp_path, monkeypatch, {"task": "- [ ] remaining"}, extra="recur: 7d\n")
    before = path.read_bytes()
    result = CliRunner().invoke(cli, ["edit", "task", "--set", "state", "done"])
    assert result.exit_code != 0, result.output
    assert path.read_bytes() == before


def test_pending_override_is_explicit(tmp_path, monkeypatch):
    (path,) = make_tasks(tmp_path, monkeypatch, {"task": "- [ ] remaining"})
    result = CliRunner().invoke(cli, ["edit", "task", "--set", "state", "done", "--allow-pending"])
    assert result.exit_code == 0, result.output
    assert "--allow-pending" in result.output
    assert frontmatter.load(path)["state"] == "done"


@pytest.mark.parametrize("state,target", [("done", "done"), ("active", "cancelled")])
def test_legacy_done_edits_and_cancellation_remain_valid(tmp_path, monkeypatch, state, target):
    (path,) = make_tasks(tmp_path, monkeypatch, {"task": "- [ ] remaining"}, state=state)
    result = CliRunner().invoke(cli, ["edit", "task", "--set", "state", target])
    assert result.exit_code == 0, result.output
    assert frontmatter.load(path)["state"] == target
