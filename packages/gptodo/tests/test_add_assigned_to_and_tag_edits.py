"""Regression tests for gptme/gptme-contrib#2304 items 3 and 10."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import frontmatter
import pytest
from click.testing import CliRunner

from gptodo import frontmatter_compat
from gptodo.cli import cli
from gptodo.lifecycle import transform_post


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "tasks").mkdir()
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "Bob"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GPTODO_AGENT_NAME", raising=False)
    return tmp_path


@pytest.mark.parametrize(
    "value",
    ["x: y", "#ops", "yes", "[team]", "bob@cluster1", "#\U0001f680ops", "a\u2028b", "a\n---\nb"],
)
def test_add_round_trips_assigned_to(workspace: Path, value: str) -> None:
    result = CliRunner().invoke(cli, ["add", "Assigned task", "--assigned-to", value], input="")
    assert result.exit_code == 0, result.output
    created = next((workspace / "tasks").glob("*.md"))
    assert frontmatter.load(created).metadata["assigned_to"] == value


def test_add_keeps_plain_assigned_to_unquoted(workspace: Path) -> None:
    CliRunner().invoke(cli, ["add", "Plain task", "--assigned-to", "agent"], input="")
    created = next((workspace / "tasks").glob("*.md"))
    assert "assigned_to: agent\n" in created.read_text()


def _post(**metadata: object) -> frontmatter.Post:
    return frontmatter.Post("# t\n", state="backlog", created="2026-11-01", **metadata)


@pytest.mark.parametrize(("tags", "expected"), [(None, ["x"]), ("infra", ["infra", "x"])])
def test_add_tag_to_null_or_scalar_tags(tags: object, expected: list[str]) -> None:
    post = transform_post(_post(tags=tags), [("add", "tags", "x")])
    assert post.metadata["tags"] == expected


def test_first_waiting_since_uses_iso_format_from_yaml_datetime() -> None:
    since = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    post = transform_post(
        _post(waiting_since=since),
        [("set", "state", "waiting"), ("set", "waiting_for", "x")],
    )
    assert post.metadata["first_waiting_since"] == since.isoformat()
    # Both fields reach disk with the same "T" separator.
    written = frontmatter_compat.frontmatter.dumps(post)
    assert f"waiting_since: {since.isoformat()}\n" in written
    assert f"first_waiting_since: '{since.isoformat()}'\n" in written
