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
        "- [-] dropped (declined: replaced)",
        "- [ ] ~~dropped~~ (see tasks/replacement.md)",
        "- [x] ~~dropped~~ (gptme/gptme#7)",
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


def test_completion_names_skipped_items(tmp_path, monkeypatch):
    (path,) = make_tasks(
        tmp_path,
        monkeypatch,
        {"task": "- [x] complete\n- [-] wire indexing (declined: replaced by scoping)"},
    )
    result = CliRunner().invoke(cli, ["edit", "task", "--set", "state", "done"])
    assert result.exit_code == 0, result.output
    assert frontmatter.load(path)["state"] == "done"
    assert "marking done with 1 skipped item(s)" in result.output
    assert "wire indexing" in result.output
    assert "no follow-up link" not in result.output


@pytest.mark.parametrize(
    ("title", "accounted"),
    [
        ("split auth (file as separate task)", False),
        ("split auth (needs its own task)", False),
        ("profile the slow path (deferred: not needed for this task)", False),
        ("profile the slow path (declined)", False),
        ("split auth (filed as tasks/split-auth.md)", True),
        ("split auth (follow-up issue gptme/gptme#12)", True),
        ("split auth (see https://example.com/issue)", True),
        ("split auth (owned by [[split-auth]])", True),
        ("split auth (declined: superseded by scoping)", True),
        ("split auth (Decided against: superseded)", True),
        ("~~split auth~~ (declined: not worth it)", True),
    ],
)
def test_completion_requires_follow_up_or_declined(tmp_path, monkeypatch, title, accounted):
    marker = "[ ]" if title.startswith("~~") else "[-]"
    (path,) = make_tasks(tmp_path, monkeypatch, {"task": f"- [x] complete\n- {marker} {title}"})
    before = path.read_bytes()
    result = CliRunner().invoke(cli, ["edit", "task", "--set", "state", "done"])
    if accounted:
        assert result.exit_code == 0, result.output
        assert frontmatter.load(path)["state"] == "done"
    else:
        assert result.exit_code != 0, result.output
        assert "no follow-up link or 'declined:'" in result.output
        assert path.read_bytes() == before


def test_unaccounted_skip_refusal_preserves_batch(tmp_path, monkeypatch):
    paths = make_tasks(
        tmp_path,
        monkeypatch,
        {"ok": "- [x] complete", "dropped": "- [x] a\n- [-] b (deferred: later)"},
    )
    before = [path.read_bytes() for path in paths]
    result = CliRunner().invoke(cli, ["edit", "ok", "dropped", "--set", "state", "done"])
    assert result.exit_code != 0, result.output
    assert "dropped" in result.output
    assert [path.read_bytes() for path in paths] == before


def test_allow_pending_overrides_unaccounted_skip(tmp_path, monkeypatch):
    (path,) = make_tasks(tmp_path, monkeypatch, {"task": "- [x] a\n- [-] b [/x] (deferred: later)"})
    result = CliRunner().invoke(cli, ["edit", "task", "--set", "state", "done", "--allow-pending"])
    assert result.exit_code == 0, result.output
    assert frontmatter.load(path)["state"] == "done"
    assert "--allow-pending" in result.output
    assert "[/x]" in result.output
    assert "no follow-up link" in result.output


def test_completion_escapes_brackets_in_skipped_titles(tmp_path, monkeypatch):
    make_tasks(
        tmp_path,
        monkeypatch,
        {"task": "- [x] complete\n- [-] split auth [/bar] see [[owner-task]] (deferred)"},
    )
    result = CliRunner().invoke(cli, ["edit", "task", "--set", "state", "done"])
    assert result.exit_code == 0, result.output
    assert "[/bar]" in result.output
    assert "[[owner-task]]" in result.output


def test_completion_without_skips_prints_no_skip_warning(tmp_path, monkeypatch):
    make_tasks(tmp_path, monkeypatch, {"task": "- [x] complete"})
    result = CliRunner().invoke(cli, ["edit", "task", "--set", "state", "done"])
    assert result.exit_code == 0, result.output
    assert "skipped item" not in result.output


def test_check_done_unresolved_lists_offenders_only(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    bodies = {
        "clean": ("done", "- [x] a"),
        "skipped": ("done", "- [x] a\n- [-] b (file as separate task)"),
        "open-done": ("done", "- [x] a\n- [ ] b"),
        "active-open": ("active", "- [ ] b"),
    }
    for name, (state, body) in bodies.items():
        (tasks_dir / f"{name}.md").write_text(
            f"---\nstate: {state}\ncreated: 2026-01-01T00:00:00+00:00\n---\n# {name}\n\n{body}\n"
        )
    result = CliRunner().invoke(cli, ["check", "--done-unresolved"])
    assert result.exit_code == 0, result.output
    assert "skipped" in result.output and "open-done" in result.output
    assert "clean" not in result.output
    assert "active-open" not in result.output
    assert "2 done task(s)" in result.output


def test_recurring_task_skips_need_no_follow_up(tmp_path, monkeypatch):
    (path,) = make_tasks(
        tmp_path,
        monkeypatch,
        {"task": "- [x] a\n- [-] b (not needed this cycle)"},
        extra="recur: 7d\n",
    )
    result = CliRunner().invoke(cli, ["edit", "task", "--set", "state", "done"])
    assert result.exit_code == 0, result.output
