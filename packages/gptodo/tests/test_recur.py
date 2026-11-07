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
    def test_done_resets_recurring_task_to_waiting(self, workspace: Path):
        write_task(workspace, "weekly-review", state="active", recur="7d")
        runner = CliRunner()
        result = runner.invoke(cli, ["edit", "weekly-review", "--set", "state", "done"])
        assert result.exit_code == 0, result.output
        assert "reset to waiting" in result.output

        post = frontmatter.load(workspace / "tasks" / "weekly-review.md")
        assert post.metadata["state"] == "waiting"
        assert "last_completed" not in post.metadata
        assert "wait" in post.metadata

    def test_done_sets_wait_to_future_for_recurring(self, workspace: Path):
        write_task(workspace, "weekly-review", state="active", recur="weekly")
        runner = CliRunner()
        runner.invoke(cli, ["edit", "weekly-review", "--set", "state", "done"])

        post = frontmatter.load(workspace / "tasks" / "weekly-review.md")
        assert post.metadata["state"] == "waiting"
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
        assert post.metadata["state"] == "waiting"
        assert "wait" in post.metadata
        future = date.fromisoformat(str(post.metadata["wait"]))
        assert future > date.today()

    def test_recurring_task_reset_emits_valid_frontmatter(self, workspace: Path):
        """Regression: recur reset emits state=waiting with validator-required fields.

        The task frontmatter validator rejects state=waiting without waiting_for
        and waiting_since. The waiting_for string must be a recurrence gate (not a
        human blocker) so the auto-releaser still surfaces the task when wait: expires.
        """
        write_task(workspace, "weekly-review", state="active", recur="7d")
        runner = CliRunner()
        result = runner.invoke(cli, ["edit", "weekly-review", "--set", "state", "done"])
        assert result.exit_code == 0, result.output

        post = frontmatter.load(workspace / "tasks" / "weekly-review.md")
        assert post.metadata["state"] == "waiting"
        assert "wait" in post.metadata
        assert post.metadata.get("wait_kind") == "machine"
        waiting_for = post.metadata.get("waiting_for")
        assert isinstance(waiting_for, str)
        assert "recurrence gate" in waiting_for
        assert str(post.metadata["wait"]) in waiting_for
        waiting_since = post.metadata.get("waiting_since")
        assert isinstance(waiting_since, str)
        # Must be a parseable ISO timestamp, not a date-only leftover.
        assert "T" in waiting_since

    def test_unknown_recur_format_marks_done_normally(self, workspace: Path):
        write_task(workspace, "mystery-task", state="active", recur="every-week")
        runner = CliRunner()
        result = runner.invoke(cli, ["edit", "mystery-task", "--set", "state", "done"])
        assert result.exit_code == 0, result.output

        post = frontmatter.load(workspace / "tasks" / "mystery-task.md")
        assert post.metadata["state"] == "done"


class TestRecurResetUnticksChecklist:
    """A recurrence reset must reopen last cycle's checklist (gordon 2026-11-07:
    a revived biweekly task showed N/N done and could be closed without the work)."""

    def test_reset_unticks_done_items(self, workspace: Path):
        path = write_task(workspace, "weekly-review", state="active", recur="7d")
        path.write_text(
            path.read_text()
            + "\n\n- [x] step one\n  - [X] nested step\n> - [x] quoted\n* [x] star bullet\n"
            "- [-] skipped (not needed this cycle)\n\n"
            "```\n- [x] example in code fence\n```\n\nprose with - [x] inline\n"
        )
        result = CliRunner().invoke(cli, ["edit", "weekly-review", "--set", "state", "done"])
        assert result.exit_code == 0, result.output

        post = frontmatter.load(path)
        assert post.metadata["state"] == "waiting"
        body = post.content
        assert "- [ ] step one" in body
        assert "  - [ ] nested step" in body
        assert "> - [ ] quoted" in body
        assert "* [ ] star bullet" in body
        assert "- [-] skipped (not needed this cycle)" in body
        assert "- [x] example in code fence" in body
        assert "prose with - [x] inline" in body

    @pytest.mark.parametrize(
        "example",
        [
            "````markdown\n```python\n- [x] example\n```\n````",
            "~~~markdown\n```\n- [x] example\n~~~",
            "> ```markdown\n> - [x] example\n> ```",
            "```markdown\n```not-a-closer\n- [x] example\n```",
            "- ```markdown\n  - [x] example\n  ```",
            "1. ~~~markdown\n   - [x] example\n   ~~~",
            "> - ```markdown\n>   - [x] example\n>   ```",
            "> > ```markdown\n> > - [x] example\n> > ```",
        ],
    )
    def test_reset_preserves_fenced_examples(self, workspace: Path, example: str):
        path = write_task(workspace, "weekly-review", state="active", recur="7d")
        path.write_text(path.read_text() + f"\n\n{example}\n\n- [x] real step\n")
        result = CliRunner().invoke(cli, ["edit", "weekly-review", "--set", "state", "done"])
        assert result.exit_code == 0, result.output
        body = frontmatter.load(path).content
        assert example in body
        assert "- [ ] real step" in body

    @pytest.mark.parametrize("inline", ["```example```", "- ```example```", "> ```example```"])
    def test_reset_does_not_treat_inline_code_as_fence(self, workspace: Path, inline: str):
        path = write_task(workspace, "weekly-review", state="active", recur="7d")
        path.write_text(path.read_text() + f"\n\n{inline}\n\n- [x] real step\n")
        result = CliRunner().invoke(cli, ["edit", "weekly-review", "--set", "state", "done"])
        assert result.exit_code == 0, result.output
        body = frontmatter.load(path).content
        assert inline in body
        assert "- [ ] real step" in body

    def test_reset_reopens_completed_emoji_items(self, workspace: Path):
        from gptodo.utils import count_subtasks

        path = write_task(workspace, "weekly-review", state="active", recur="7d")
        path.write_text(
            path.read_text() + "\n\n- ✅ shipped\n  - ✅ nested\n> - ✅ quoted\n* ✅ star bullet\n"
            "\n```markdown\n- ✅ example\n```\n\nprose with - ✅ inline\n"
        )
        result = CliRunner().invoke(cli, ["edit", "weekly-review", "--set", "state", "done"])
        assert result.exit_code == 0, result.output

        post = frontmatter.load(path)
        assert post.metadata["state"] == "waiting"
        assert "- [ ] shipped" in post.content
        assert "  - [ ] nested" in post.content
        assert "> - [ ] quoted" in post.content
        assert "* [ ] star bullet" in post.content
        assert "```markdown\n- ✅ example\n```" in post.content
        assert "prose with - ✅ inline" in post.content
        assert count_subtasks(post.content, checklist_only=True).completed == 0

    @pytest.mark.parametrize(
        "example",
        [
            "- ```markdown\n  - [x] example",
            "1. ~~~markdown\n   - [x] example",
            "> ```markdown\n> - [x] example",
            "> - ```markdown\n>   - [x] example",
            "> > ```markdown\n> > - [x] example",
            "- ```markdown\n\n  - [x] example",
            "1. ~~~markdown\n\n   - [x] example",
            "- notes\n\n  ```markdown\n  - [x] example",
            "1. notes\n\n   ~~~markdown\n   - [x] example",
            "> - notes\n>\n>   ```markdown\n>   - [x] example",
        ],
    )
    def test_reset_ends_fence_at_container_boundary(self, workspace: Path, example: str):
        path = write_task(workspace, "weekly-review", state="active", recur="7d")
        path.write_text(path.read_text() + f"\n\n{example}\n\n- [x] real step\n")
        result = CliRunner().invoke(cli, ["edit", "weekly-review", "--set", "state", "done"])
        assert result.exit_code == 0, result.output
        post = frontmatter.load(path)
        assert post.metadata["state"] == "waiting"
        assert example in post.content
        assert "- [ ] real step" in post.content

    @pytest.mark.parametrize("marker", ["1.", "2)", "-", "*", "+"])
    def test_reset_reopens_list_markers(self, workspace: Path, marker: str):
        path = write_task(workspace, "weekly-review", state="active", recur="7d")
        path.write_text(path.read_text() + f"\n\n{marker}  [x] real step\n")
        result = CliRunner().invoke(cli, ["edit", "weekly-review", "--set", "state", "done"])
        assert result.exit_code == 0, result.output
        assert f"{marker}  [ ] real step" in frontmatter.load(path).content

    def test_non_recurring_done_keeps_ticks(self, workspace: Path):
        path = write_task(workspace, "one-off", state="active")
        path.write_text(path.read_text() + "\n\n- [x] did it\n")
        result = CliRunner().invoke(cli, ["edit", "one-off", "--set", "state", "done"])
        assert result.exit_code == 0, result.output
        assert "- [x] did it" in frontmatter.load(path).content

    def test_cron_recur_keeps_ticks(self, workspace: Path):
        path = write_task(workspace, "cron-task", state="active", recur='"0 9 * * 1"')
        path.write_text(path.read_text() + "\n\n- [x] did it\n")
        result = CliRunner().invoke(cli, ["edit", "cron-task", "--set", "state", "done"])
        assert result.exit_code == 0, result.output
        post = frontmatter.load(path)
        assert post.metadata["state"] == "done"
        assert "- [x] did it" in post.content
