"""Tests for the gptodo frontmatter compatibility shim."""

from __future__ import annotations

from pathlib import Path

from gptodo.frontmatter_compat import frontmatter


def test_loads_frontmatter_file(tmp_path: Path) -> None:
    task = tmp_path / "task.md"
    task.write_text(
        "---\nstate: waiting\ncreated: 2026-04-06T12:00:00+00:00\ntags:\n  - alice\n---\n# Task\n",
        encoding="utf-8",
    )

    post = frontmatter.load(task)

    assert post.metadata["state"] == "waiting"
    assert post.metadata["tags"] == ["alice"]
    assert post.content == "# Task\n"


def test_dumps_roundtrips_post_metadata() -> None:
    post = frontmatter.Post(content="# Task\n", state="done", created="2026-04-06")

    rendered = frontmatter.dumps(post)
    reparsed = frontmatter.loads(rendered)

    assert reparsed.metadata == {"state": "done", "created": "2026-04-06"}
    assert reparsed.content == "# Task\n"
