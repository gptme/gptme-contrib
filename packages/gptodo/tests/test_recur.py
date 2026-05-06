"""Tests for gptodo recur (recurring task) feature."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.frontmatter_compat import frontmatter
from gptodo.utils import parse_recur_interval


# =============================================================================
# parse_recur_interval
# =============================================================================


class TestParseRecurInterval:
    def test_days_format(self):
        assert parse_recur_interval("7d") == timedelta(days=7)
        assert parse_recur_interval("14d") == timedelta(days=14)
        assert parse_recur_interval("30d") == timedelta(days=30)
        assert parse_recur_interval("1d") == timedelta(days=1)

    def test_named_weekly(self):
        assert parse_recur_interval("weekly") == timedelta(days=7)

    def test_named_monthly(self):
        assert parse_recur_interval("monthly") == timedelta(days=30)

    def test_case_insensitive(self):
        assert parse_recur_interval("WEEKLY") == timedelta(days=7)
        assert parse_recur_interval("Monthly") == timedelta(days=30)
        assert parse_recur_interval("7D") == timedelta(days=7)

    def test_whitespace_stripped(self):
        assert parse_recur_interval("  7d  ") == timedelta(days=7)

    def test_unknown_format_returns_none(self):
        assert parse_recur_interval("") is None
        assert parse_recur_interval("7w") is None
        assert parse_recur_interval("every week") is None
        assert parse_recur_interval("0 9 * * 1") is None


# =============================================================================
# CLI integration: marking a recurring task done resets it
# =============================================================================


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "tasks").mkdir()
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "gordon"\n')
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write_task(workspace: Path, name: str, **metadata: object) -> Path:
    """Write a task file with frontmatter (no repr quoting — plain YAML values)."""
    lines = ["---"]
    for key, value in metadata.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append(f"# {name}")
    path = workspace / "tasks" / f"{name}.md"
    path.write_text("\n".join(lines))
    return path


class TestRecurCliReset:
    def test_done_resets_recurring_task_to_todo(self, workspace: Path):
        write_task(workspace, "weekly-review", state="active", recur="7d")
        runner = CliRunner()
        result = runner.invoke(cli, ["edit", "weekly-review", "--set", "state", "done"])
        assert result.exit_code == 0, result.output
        assert "reset to todo" in result.output

        post = frontmatter.load(workspace / "tasks" / "weekly-review.md")
        assert post.metadata["state"] == "todo"
        assert "last_completed" not in post.metadata
        assert "wait" in post.metadata

    def test_done_sets_wait_to_future_for_recurring(self, workspace: Path):
        write_task(workspace, "weekly-review", state="active", recur="weekly")
        runner = CliRunner()
        runner.invoke(cli, ["edit", "weekly-review", "--set", "state", "done"])

        post = frontmatter.load(workspace / "tasks" / "weekly-review.md")
        assert post.metadata["state"] == "todo"
        assert "wait" in post.metadata
        tomorrow = date.today() + timedelta(days=1)
        assert date.fromisoformat(str(post.metadata["wait"])) >= tomorrow

    def test_done_non_recurring_task_stays_done(self, workspace: Path):
        write_task(workspace, "one-off-task", state="active")
        runner = CliRunner()
        result = runner.invoke(cli, ["edit", "one-off-task", "--set", "state", "done"])
        assert result.exit_code == 0, result.output

        post = frontmatter.load(workspace / "tasks" / "one-off-task.md")
        assert post.metadata["state"] == "done"
        assert "last_completed" not in post.metadata

    def test_recurring_task_gets_wait_after_done(self, workspace: Path):
        write_task(workspace, "weekly-review", state="active", recur="7d")
        runner = CliRunner()
        runner.invoke(cli, ["edit", "weekly-review", "--set", "state", "done"])

        post = frontmatter.load(workspace / "tasks" / "weekly-review.md")
        assert post.metadata["state"] == "todo"
        assert "wait" in post.metadata
        future = date.fromisoformat(str(post.metadata["wait"]))
        assert future > date.today()

    def test_unknown_recur_format_marks_done_normally(self, workspace: Path):
        write_task(workspace, "mystery-task", state="active", recur="every-week")
        runner = CliRunner()
        result = runner.invoke(cli, ["edit", "mystery-task", "--set", "state", "done"])
        assert result.exit_code == 0, result.output

        post = frontmatter.load(workspace / "tasks" / "mystery-task.md")
        assert post.metadata["state"] == "done"
