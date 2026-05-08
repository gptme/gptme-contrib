"""Tests for the gptodo frontmatter compatibility shim."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from gptodo.frontmatter_compat import Post, dumps, frontmatter, load, loads


def test_loads_frontmatter_file(tmp_path: Path) -> None:
    task = tmp_path / "task.md"
    task.write_text(
        "---\nstate: waiting\ncreated: 2026-04-06T12:00:00+00:00\ntags:\n  - alice\n---\n# Task\n",
        encoding="utf-8",
    )

    post = load(task)

    assert post.metadata["state"] == "waiting"
    assert post.metadata["tags"] == ["alice"]
    assert post.content == "# Task\n"


def test_dumps_roundtrips_post_metadata() -> None:
    post = Post(content="# Task\n", state="done", created="2026-04-06")

    rendered = dumps(post)
    reparsed = loads(rendered)

    assert reparsed.metadata == {"state": "done", "created": "2026-04-06"}
    assert reparsed.content == "# Task\n"


def test_datetime_round_trip_preserves_iso_t_separator() -> None:
    """A YAML datetime value must round-trip with the 'T' separator intact.

    PyYAML's default timestamp serialization uses a space, which the workspace
    task validator rejects. This test guards against the regression where a
    `frontmatter.load -> frontmatter.dumps` cycle silently rewrites
    `2026-05-01T09:31:00+00:00` as `2026-05-01 09:31:00+00:00`.
    """
    text = (
        "---\n"
        "created: 2026-05-01T09:31:00+00:00\n"
        "state: waiting\n"
        "waiting_since: 2026-05-02\n"
        "---\n"
        "body\n"
    )
    post = frontmatter.loads(text)
    out = frontmatter.dumps(post)

    assert "2026-05-01T09:31:00" in out, f"datetime T separator lost: {out!r}"
    assert "2026-05-02" in out


def test_safe_dump_datetime_uses_t_separator() -> None:
    """yaml.safe_dump must also use the T separator after the representer is registered."""
    rendered = yaml.safe_dump({"created": datetime(2026, 5, 1, 9, 31, 0, tzinfo=timezone.utc)})
    assert "2026-05-01T09:31:00" in rendered, rendered


def test_date_round_trip_stays_iso() -> None:
    """A bare date must serialize as YYYY-MM-DD (no spurious time component)."""
    post = Post(content="body", waiting_since=date(2026, 5, 2))
    rendered = dumps(post)
    assert "waiting_since: 2026-05-02" in rendered
