"""Tests for gptodo recur (recurring task) feature."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.frontmatter_compat import frontmatter
from gptodo.utils import parse_recur_interval, task_is_recur_blocked


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
# task_is_recur_blocked — tested via duck-typed metadata holder
# =============================================================================


def _meta_task(recur: str | None = None, last_completed: str | None = None):
    """Return a duck-typed object with only the .metadata attribute needed by task_is_recur_blocked."""
    metadata: dict = {"state": "active"}
    if recur is not None:
        metadata["recur"] = recur
    if last_completed is not None:
        metadata["last_completed"] = last_completed
    return SimpleNamespace(metadata=metadata)


class TestTaskIsRecurBlocked:
    def test_no_recur_not_blocked(self):
        assert not task_is_recur_blocked(_meta_task())

    def test_recur_without_last_completed_not_blocked(self):
        assert not task_is_recur_blocked(_meta_task(recur="7d"))

    def test_recently_completed_is_blocked(self):
        yesterday = (datetime.now() - timedelta(days=1)).isoformat()
        assert task_is_recur_blocked(_meta_task(recur="7d", last_completed=yesterday))

    def test_overdue_is_not_blocked(self):
        eight_days_ago = (datetime.now() - timedelta(days=8)).isoformat()
        assert not task_is_recur_blocked(_meta_task(recur="7d", last_completed=eight_days_ago))

    def test_exactly_at_boundary_not_blocked(self):
        seven_days_plus_one_sec_ago = (datetime.now() - timedelta(days=7, seconds=1)).isoformat()
        assert not task_is_recur_blocked(
            _meta_task(recur="7d", last_completed=seven_days_plus_one_sec_ago)
        )

    def test_weekly_alias(self):
        yesterday = (datetime.now() - timedelta(days=1)).isoformat()
        assert task_is_recur_blocked(_meta_task(recur="weekly", last_completed=yesterday))

    def test_monthly_alias(self):
        ten_days_ago = (datetime.now() - timedelta(days=10)).isoformat()
        assert task_is_recur_blocked(_meta_task(recur="monthly", last_completed=ten_days_ago))

    def test_unknown_recur_format_not_blocked(self):
        yesterday = (datetime.now() - timedelta(days=1)).isoformat()
        assert not task_is_recur_blocked(_meta_task(recur="every-week", last_completed=yesterday))

    def test_date_only_last_completed(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        assert task_is_recur_blocked(_meta_task(recur="7d", last_completed=yesterday))


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
    def test_done_resets_recurring_task_to_active(self, workspace: Path):
        write_task(workspace, "weekly-review", state="active", recur="7d")
        runner = CliRunner()
        result = runner.invoke(cli, ["edit", "weekly-review", "--set", "state", "done"])
        assert result.exit_code == 0, result.output
        assert "Recurring task reset" in result.output

        post = frontmatter.load(workspace / "tasks" / "weekly-review.md")
        assert post.metadata["state"] == "active"
        assert post.metadata["last_completed"] == date.today().isoformat()

    def test_done_sets_last_completed_to_today(self, workspace: Path):
        write_task(workspace, "weekly-review", state="active", recur="weekly")
        runner = CliRunner()
        runner.invoke(cli, ["edit", "weekly-review", "--set", "state", "done"])

        post = frontmatter.load(workspace / "tasks" / "weekly-review.md")
        assert post.metadata["last_completed"] == date.today().isoformat()

    def test_done_non_recurring_task_stays_done(self, workspace: Path):
        write_task(workspace, "one-off-task", state="active")
        runner = CliRunner()
        result = runner.invoke(cli, ["edit", "one-off-task", "--set", "state", "done"])
        assert result.exit_code == 0, result.output

        post = frontmatter.load(workspace / "tasks" / "one-off-task.md")
        assert post.metadata["state"] == "done"
        assert "last_completed" not in post.metadata

    def test_recurring_task_blocked_after_reset(self, workspace: Path):
        write_task(workspace, "weekly-review", state="active", recur="7d")
        runner = CliRunner()
        runner.invoke(cli, ["edit", "weekly-review", "--set", "state", "done"])

        post = frontmatter.load(workspace / "tasks" / "weekly-review.md")
        task_meta = SimpleNamespace(metadata=dict(post.metadata))
        assert task_is_recur_blocked(task_meta)

    def test_unknown_recur_format_marks_done_normally(self, workspace: Path):
        write_task(workspace, "mystery-task", state="active", recur="every-week")
        runner = CliRunner()
        result = runner.invoke(cli, ["edit", "mystery-task", "--set", "state", "done"])
        assert result.exit_code == 0, result.output

        post = frontmatter.load(workspace / "tasks" / "mystery-task.md")
        assert post.metadata["state"] == "done"
