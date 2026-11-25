"""mutate_task must not rewrite what it did not change (gptme/gptme-contrib#2304 item 2)."""

from datetime import datetime
from pathlib import Path

from gptodo.frontmatter_compat import frontmatter, render_update
from gptodo.lifecycle import mutate_task

TASK = (
    "---\n"
    "state: todo\n"
    "# owner note: keep this\n"
    "created: 2026-01-01T00:00:00+00:00\n"
    "priority: low\n"
    "next_action: 'A long quoted value that wraps\n"
    "  onto a second line'\n"
    "tags:\n"
    "- infra\n"
    "- gptodo  # keep the inline comment\n"
    "---\n"
    "\n"
    "# Title\n"
    "\n"
    "Body line with trailing spaces   \n"
)


def _write(tmp_path: Path, text: str = TASK) -> Path:
    path = tmp_path / "task.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_metadata_change_keeps_order_comments_and_body(tmp_path: Path) -> None:
    path = _write(tmp_path)
    mutate_task(path, patch={"priority": "high"})
    assert path.read_text(encoding="utf-8") == TASK.replace("priority: low", "priority: high")


def test_added_key_is_appended_and_removed_key_dropped(tmp_path: Path) -> None:
    path = _write(tmp_path)
    mutate_task(path, patch={"next_action": None, "assigned_to": "bob"})
    text = path.read_text(encoding="utf-8")
    assert "next_action" not in text
    assert text.index("tags:") < text.index("assigned_to: bob")
    assert "# owner note: keep this" in text
    assert "- gptodo  # keep the inline comment" in text
    assert text.endswith("Body line with trailing spaces   \n")


def test_body_change_keeps_metadata_lines(tmp_path: Path) -> None:
    path = _write(tmp_path)
    mutate_task(path, prepare=lambda post: [("set_body", "", post.content + "\n\nMore.")])
    text = path.read_text(encoding="utf-8")
    head = TASK.split("---\n", 2)[1]
    assert text.startswith(f"---\n{head}---\n\n# Title")
    assert text.endswith("More.\n")
    assert frontmatter.loads(text).metadata == frontmatter.loads(TASK).metadata


def test_unparseable_layout_falls_back_to_ordered_dump() -> None:
    raw = '---\n"quoted key": 1\nstate: todo\n---\nbody\n'
    old = frontmatter.loads(raw)
    new = frontmatter.loads(raw)
    new.metadata["state"] = "active"
    text = render_update(raw, old, new)
    loaded = frontmatter.loads(text)
    assert list(loaded.metadata) == ["quoted key", "state"]
    assert loaded.metadata["state"] == "active"


def test_bool_and_int_are_not_treated_as_unchanged() -> None:
    raw = "---\nflag: 1\nstate: todo\n---\nbody\n"
    old = frontmatter.loads(raw)
    new = frontmatter.loads(raw)
    new.metadata["flag"] = True
    assert "flag: true" in render_update(raw, old, new)


def test_new_datetime_value_uses_iso_separator() -> None:
    raw = "---\nstate: todo\n---\nbody\n"
    old = frontmatter.loads(raw)
    new = frontmatter.loads(raw)
    new.metadata["waiting_since"] = datetime.fromisoformat("2026-11-25T04:00:00+00:00")
    assert "waiting_since: 2026-11-25T04:00:00+00:00" in render_update(raw, old, new)
