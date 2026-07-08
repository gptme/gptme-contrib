"""Tests for the gptodo browse command."""

import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import frontmatter
from click.testing import CliRunner

from gptodo.cli import cli, _get_browse_lines, _write_browse_scripts
from gptodo.utils import load_tasks as load_tasks_util


def create_task(
    tasks_dir: Path,
    name: str,
    state: str,
    priority: str = "medium",
    project: str | None = None,
    content: str = "Task body content.",
    created: str | None = None,
    extra_meta: dict | None = None,
):
    """Helper to create a task markdown file with frontmatter."""
    meta = {"state": state, "priority": priority}
    if project:
        meta["project"] = project
    if created:
        meta["created"] = created
    if extra_meta:
        meta.update(extra_meta)
    post = frontmatter.Post(content, **meta)
    task_file = tasks_dir / f"{name}.md"
    task_file.write_text(frontmatter.dumps(post))
    return task_file


def _fzf_side_effect(mock_result):
    """Create a side_effect function that intercepts fzf subprocess calls."""
    original_run = subprocess.run

    def side_effect(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and cmd and cmd[0] == "fzf":
            return mock_result
        return original_run(*args, **kwargs)

    return side_effect


def _get_fzf_calls(mock_run):
    """Extract fzf calls from a mock subprocess.run."""
    return [
        c
        for c in mock_run.call_args_list
        if isinstance(c[0][0], list) and c[0][0][0] == "fzf" and c[0][0][1:] != ["--version"]
    ]


def _get_fzf_arg(fzf_cmd, arg_name):
    """Extract the value of a specific fzf argument from the command list."""
    for i, a in enumerate(fzf_cmd):
        if a == arg_name and i + 1 < len(fzf_cmd):
            return fzf_cmd[i + 1]
    return None


class TestBrowseNoTasks:
    """Test browse with no tasks available."""

    def test_browse_no_tasks_dir(self, tmp_path, monkeypatch):
        """Should show error when no tasks directory exists."""
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tmp_path / "tasks"))
        runner = CliRunner()
        result = runner.invoke(cli, ["browse", "--no-fzf"])
        assert result.exit_code == 0
        assert "No tasks found" in result.output

    def test_browse_empty_tasks_dir(self, tmp_path, monkeypatch):
        """Should show error when tasks directory is empty."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))
        runner = CliRunner()
        result = runner.invoke(cli, ["browse", "--no-fzf"])
        assert result.exit_code == 0
        assert "No tasks found" in result.output


class TestBrowseDefaultFilter:
    """Test that browse defaults to current open-work states."""

    def test_browse_active_only_default(self, tmp_path, monkeypatch):
        """Default browse should show current open-work states."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "task-active", "active")
        create_task(tasks_dir, "task-backlog", "backlog")
        create_task(tasks_dir, "task-todo", "todo")
        create_task(tasks_dir, "task-review", "ready_for_review")
        create_task(tasks_dir, "task-waiting", "waiting")
        create_task(tasks_dir, "task-done", "done")
        create_task(tasks_dir, "task-cancelled", "cancelled")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        runner = CliRunner()
        result = runner.invoke(cli, ["browse", "--no-fzf"])
        assert "task-active" in result.output
        assert "task-backlog" in result.output
        assert "task-todo" in result.output
        assert "task-review" in result.output
        assert "task-waiting" in result.output
        assert "task-done" not in result.output
        assert "task-cancelled" not in result.output


class TestBrowseAllFlag:
    """Test --all flag includes done/cancelled tasks."""

    def test_browse_all_flag(self, tmp_path, monkeypatch):
        """--all should include done and cancelled tasks."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "task-active", "active")
        create_task(tasks_dir, "task-done", "done")
        create_task(tasks_dir, "task-cancelled", "cancelled")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        runner = CliRunner()
        result = runner.invoke(cli, ["browse", "--all", "--no-fzf"])
        assert "task-active" in result.output
        assert "task-done" in result.output
        assert "task-cancelled" in result.output


class TestBrowseProjectFilter:
    """Test --project filter."""

    def test_browse_project_filter(self, tmp_path, monkeypatch):
        """--project should only show tasks from that project."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "task-gptme", "active", project="gptme")
        create_task(tasks_dir, "task-other", "active", project="other")
        create_task(tasks_dir, "task-none", "active")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        runner = CliRunner()
        result = runner.invoke(cli, ["browse", "--project", "gptme", "--no-fzf"])
        assert "task-gptme" in result.output
        assert "task-other" not in result.output
        assert "task-none" not in result.output

    def test_browse_project_no_match(self, tmp_path, monkeypatch):
        """--project with no matches should show message."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "task-other", "active", project="other")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        runner = CliRunner()
        result = runner.invoke(cli, ["browse", "--project", "nonexistent", "--no-fzf"])
        assert "No tasks found" in result.output or "no" in result.output.lower()


class TestBrowseStateFilter:
    """Test --state filter."""

    def test_browse_state_filter(self, tmp_path, monkeypatch):
        """--state should only show tasks with that state."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "task-active", "active")
        create_task(tasks_dir, "task-backlog", "backlog")
        create_task(tasks_dir, "task-waiting", "waiting")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        runner = CliRunner()
        result = runner.invoke(cli, ["browse", "--state", "active", "--no-fzf"])
        assert "task-active" in result.output
        assert "task-backlog" not in result.output
        assert "task-waiting" not in result.output

    def test_browse_state_filter_ready_for_review(self, tmp_path, monkeypatch):
        """--state should accept ready_for_review."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "task-review", "ready_for_review")
        create_task(tasks_dir, "task-active", "active")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        runner = CliRunner()
        result = runner.invoke(cli, ["browse", "--state", "ready_for_review", "--no-fzf"])
        assert result.exit_code == 0
        assert "task-review" in result.output
        assert "task-active" not in result.output

    def test_browse_state_filter_ready_for_review_alias(self, tmp_path, monkeypatch):
        """--state should accept ready-for-review as a CLI alias."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "task-review", "ready_for_review")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        runner = CliRunner()
        result = runner.invoke(cli, ["browse", "--state", "ready-for-review", "--no-fzf"])
        assert result.exit_code == 0
        assert "task-review" in result.output


class TestBrowsePagerFallback:
    """Test pager fallback when fzf is unavailable."""

    def test_browse_fzf_unavailable_falls_back_to_pager(self, tmp_path, monkeypatch):
        """When fzf is not installed, should use pager output."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "my-task", "active", content="This is task content.")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        with patch("shutil.which", return_value=None):
            runner = CliRunner()
            result = runner.invoke(cli, ["browse"])
            # Should not crash, should show content
            assert result.exit_code == 0
            assert "my-task" in result.output

    def test_browse_no_fzf_flag_forces_pager(self, tmp_path, monkeypatch):
        """--no-fzf should use pager even when fzf is available."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "my-task", "active", content="Pager content here.")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        with patch("shutil.which", return_value="/usr/bin/fzf"):
            runner = CliRunner()
            result = runner.invoke(cli, ["browse", "--no-fzf"])
            assert result.exit_code == 0
            assert "my-task" in result.output


class TestBrowsePagerContentFormat:
    """Test that pager output has proper formatting."""

    def test_browse_pager_content_format(self, tmp_path, monkeypatch):
        """Pager output should contain task name, state, and content with separators."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "alpha-task", "active", content="Alpha body text.")
        create_task(tasks_dir, "beta-task", "backlog", content="Beta body text.")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        runner = CliRunner()
        result = runner.invoke(cli, ["browse", "--no-fzf"])
        output = result.output

        # Should contain both task names
        assert "alpha-task" in output
        assert "beta-task" in output
        # Should contain task content bodies
        assert "Alpha body text." in output
        assert "Beta body text." in output
        # Should contain separators (visual dividers between tasks)
        assert "═" in output or "---" in output or "===" in output


class TestBrowseFzfMode:
    """Test fzf interactive mode."""

    def test_browse_fzf_available(self, tmp_path, monkeypatch):
        """When fzf is available, should invoke subprocess with fzf."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "my-task", "active")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        mock_result = MagicMock()
        mock_result.returncode = 130  # User cancelled with Esc
        mock_result.stdout = ""

        with (
            patch("shutil.which", return_value="/usr/bin/fzf"),
            patch("subprocess.run", side_effect=_fzf_side_effect(mock_result)) as mock_run,
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["browse"])
            assert result.exit_code == 0
            assert len(_get_fzf_calls(mock_run)) == 1

    def test_browse_fzf_preview_command(self, tmp_path, monkeypatch):
        """fzf should be called with --preview containing gptodo show."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "my-task", "active")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        mock_result = MagicMock()
        mock_result.returncode = 130
        mock_result.stdout = ""

        with (
            patch("shutil.which", return_value="/usr/bin/fzf"),
            patch("subprocess.run", side_effect=_fzf_side_effect(mock_result)) as mock_run,
        ):
            runner = CliRunner()
            runner.invoke(cli, ["browse"])
            fzf_calls = _get_fzf_calls(mock_run)
            assert len(fzf_calls) == 1
            fzf_cmd = fzf_calls[0][0][0]
            preview = _get_fzf_arg(fzf_cmd, "--preview")
            assert preview is not None
            assert "gptodo" in preview and "show" in preview

    def test_browse_fzf_selection_prints_task_id(self, tmp_path, monkeypatch):
        """When user selects a task in fzf, should print the task ID."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "selected-task", "active")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        mock_fzf_result = MagicMock()
        mock_fzf_result.returncode = 0
        mock_fzf_result.stdout = "selected-task  🏃 active  🟡      3d"

        with (
            patch("shutil.which", return_value="/usr/bin/fzf"),
            patch("subprocess.run", side_effect=_fzf_side_effect(mock_fzf_result)),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["browse"])
            assert "selected-task" in result.output


class TestBrowseFzfKeyHints:
    """Test that fzf border label contains keybinding hints."""

    def test_browse_fzf_border_label_contains_hints(self, tmp_path, monkeypatch):
        """fzf should have a border-label with keybinding hints."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "my-task", "active")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        mock_result = MagicMock()
        mock_result.returncode = 130
        mock_result.stdout = ""

        with (
            patch("shutil.which", return_value="/usr/bin/fzf"),
            patch("subprocess.run", side_effect=_fzf_side_effect(mock_result)) as mock_run,
        ):
            runner = CliRunner()
            runner.invoke(cli, ["browse"])
            fzf_calls = _get_fzf_calls(mock_run)
            fzf_cmd = fzf_calls[0][0][0]
            label = _get_fzf_arg(fzf_cmd, "--border-label")
            assert label is not None
            # Should contain the command palette hint
            assert "?" in label
            # Should contain key hints for common actions
            assert "Sort" in label
            assert "Filter" in label
            assert "Edit" in label
            assert "Preview" in label
            assert "Raw" in label
            assert "Layout" in label

    def test_browse_fzf_border_label_at_bottom(self, tmp_path, monkeypatch):
        """Border label should be positioned at bottom."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "my-task", "active")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        mock_result = MagicMock()
        mock_result.returncode = 130
        mock_result.stdout = ""

        with (
            patch("shutil.which", return_value="/usr/bin/fzf"),
            patch("subprocess.run", side_effect=_fzf_side_effect(mock_result)) as mock_run,
        ):
            runner = CliRunner()
            runner.invoke(cli, ["browse"])
            fzf_calls = _get_fzf_calls(mock_run)
            fzf_cmd = fzf_calls[0][0][0]
            pos = _get_fzf_arg(fzf_cmd, "--border-label-pos")
            assert pos is not None
            assert "bottom" in pos


class TestBrowseFzfBindings:
    """Test that fzf keybindings are correctly configured."""

    def _get_bindings(self, tmp_path, monkeypatch):
        """Helper to invoke browse and return the --bind string."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "my-task", "active")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        mock_result = MagicMock()
        mock_result.returncode = 130
        mock_result.stdout = ""

        with (
            patch("shutil.which", return_value="/usr/bin/fzf"),
            patch("subprocess.run", side_effect=_fzf_side_effect(mock_result)) as mock_run,
        ):
            runner = CliRunner()
            runner.invoke(cli, ["browse"])
            fzf_calls = _get_fzf_calls(mock_run)
            fzf_cmd = fzf_calls[0][0][0]
            return _get_fzf_arg(fzf_cmd, "--bind")

    def test_browse_fzf_command_palette_binding(self, tmp_path, monkeypatch):
        """fzf --bind should include ?:execute(...) for the command palette."""
        bindings = self._get_bindings(tmp_path, monkeypatch)
        assert bindings is not None
        assert "?:execute(" in bindings
        assert "palette.sh" in bindings

    def test_browse_fzf_sort_binding(self, tmp_path, monkeypatch):
        """fzf --bind should include ctrl-s for sort picker."""
        bindings = self._get_bindings(tmp_path, monkeypatch)
        assert "ctrl-s:execute(" in bindings
        assert "sort-picker.sh" in bindings

    def test_browse_fzf_filter_binding(self, tmp_path, monkeypatch):
        """fzf --bind should include ctrl-f for filter picker."""
        bindings = self._get_bindings(tmp_path, monkeypatch)
        assert "ctrl-f:execute(" in bindings
        assert "filter-picker.sh" in bindings

    def test_browse_fzf_state_change_binding(self, tmp_path, monkeypatch):
        """fzf --bind should include ctrl-t for state change."""
        bindings = self._get_bindings(tmp_path, monkeypatch)
        assert "ctrl-t:execute(" in bindings
        assert "state-change.sh" in bindings

    def test_browse_fzf_edit_binding(self, tmp_path, monkeypatch):
        """fzf --bind should include ctrl-e with $EDITOR."""
        bindings = self._get_bindings(tmp_path, monkeypatch)
        assert "ctrl-e:execute(" in bindings
        assert "edit-task.sh" in bindings

    def test_browse_edit_helper_uses_nano_with_tty(self, tmp_path):
        """Browse edit helper should default to nano and attach to /dev/tty."""
        state_dir = tmp_path / "browse-state"
        state_dir.mkdir()

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / "tasks").mkdir()

        _write_browse_scripts(state_dir, repo_root)
        edit_script = (state_dir / "edit-task.sh").read_text()
        palette_script = (state_dir / "palette.sh").read_text()

        assert "${EDITOR:-nano}" in edit_script
        assert "</dev/tty >/dev/tty 2>/dev/tty" in edit_script
        assert "edit-task.sh" in palette_script

    def test_browse_fzf_blame_binding(self, tmp_path, monkeypatch):
        """fzf --bind should include ctrl-b with git blame."""
        bindings = self._get_bindings(tmp_path, monkeypatch)
        assert "ctrl-b:change-preview(" in bindings
        assert "git" in bindings and "blame" in bindings

    def test_browse_fzf_log_binding(self, tmp_path, monkeypatch):
        """fzf --bind should include ctrl-l with git log."""
        bindings = self._get_bindings(tmp_path, monkeypatch)
        assert "ctrl-l:change-preview(" in bindings
        assert "git" in bindings and "log" in bindings

    def test_browse_fzf_preview_reset_binding(self, tmp_path, monkeypatch):
        """fzf --bind should include ctrl-p to reset preview with rendered markdown."""
        bindings = self._get_bindings(tmp_path, monkeypatch)
        assert "ctrl-p:change-preview(" in bindings
        assert "gptodo show --render" in bindings

    def test_browse_fzf_layout_toggle_binding(self, tmp_path, monkeypatch):
        """fzf --bind should include ctrl-w to toggle preview layout."""
        bindings = self._get_bindings(tmp_path, monkeypatch)
        assert "ctrl-w:change-preview-window(" in bindings
        assert "down:" in bindings
        assert "right:" in bindings

    def test_browse_fzf_toggle_preview_binding(self, tmp_path, monkeypatch):
        """fzf --bind should include ctrl-/ to toggle preview."""
        bindings = self._get_bindings(tmp_path, monkeypatch)
        assert "ctrl-/:toggle-preview" in bindings


class TestBrowseListCommand:
    """Test the browse-list hidden subcommand."""

    def test_browse_list_output_format(self, tmp_path, monkeypatch):
        """browse-list should produce header + space-aligned data lines."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "my-task", "active", priority="high", project="myproj")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        runner = CliRunner()
        result = runner.invoke(cli, ["browse-list"])
        assert result.exit_code == 0
        lines = result.output.strip().split("\n")
        assert len(lines) == 2  # header + 1 data line
        # Header row has column names
        assert "NAME" in lines[0]
        assert "STATE" in lines[0]
        assert "CREATED" in lines[0]
        # Data line has task name as first field
        fields = lines[1].split()
        assert fields[0] == "my-task"
        assert "active" in lines[1]
        assert "myproj" in lines[1]

    def test_browse_list_default_filter(self, tmp_path, monkeypatch):
        """browse-list should default to current open-work states."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "task-active", "active")
        create_task(tasks_dir, "task-backlog", "backlog")
        create_task(tasks_dir, "task-todo", "todo")
        create_task(tasks_dir, "task-review", "ready_for_review")
        create_task(tasks_dir, "task-done", "done")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        runner = CliRunner()
        result = runner.invoke(cli, ["browse-list"])
        assert "task-active" in result.output
        assert "task-backlog" in result.output
        assert "task-todo" in result.output
        assert "task-review" in result.output
        assert "task-done" not in result.output

    def test_browse_list_all_flag(self, tmp_path, monkeypatch):
        """browse-list --all should include done/cancelled tasks."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "task-active", "active")
        create_task(tasks_dir, "task-done", "done")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        runner = CliRunner()
        result = runner.invoke(cli, ["browse-list", "--all"])
        assert "task-active" in result.output
        assert "task-done" in result.output

    def test_browse_list_state_filter(self, tmp_path, monkeypatch):
        """browse-list --state should filter by state."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "task-active", "active")
        create_task(tasks_dir, "task-backlog", "backlog")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        runner = CliRunner()
        result = runner.invoke(cli, ["browse-list", "--state", "active"])
        assert "task-active" in result.output
        assert "task-backlog" not in result.output

    def test_browse_list_project_filter(self, tmp_path, monkeypatch):
        """browse-list --project should filter by project."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "task-gptme", "active", project="gptme")
        create_task(tasks_dir, "task-other", "active", project="other")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        runner = CliRunner()
        result = runner.invoke(cli, ["browse-list", "--project", "gptme"])
        assert "task-gptme" in result.output
        assert "task-other" not in result.output


class TestBrowseListSort:
    """Test browse-list sort modes."""

    def test_browse_list_sort_priority(self, tmp_path, monkeypatch):
        """--sort priority should order high before medium before low."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "task-low", "active", priority="low")
        create_task(tasks_dir, "task-high", "active", priority="high")
        create_task(tasks_dir, "task-medium", "active", priority="medium")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        runner = CliRunner()
        result = runner.invoke(cli, ["browse-list", "--sort", "priority"])
        lines = result.output.strip().split("\n")
        task_names = [line.split()[0] for line in lines[1:]]  # skip header
        assert task_names == ["task-high", "task-medium", "task-low"]

    def test_browse_list_sort_name(self, tmp_path, monkeypatch):
        """--sort name should order alphabetically."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "charlie", "active")
        create_task(tasks_dir, "alpha", "active")
        create_task(tasks_dir, "bravo", "active")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        runner = CliRunner()
        result = runner.invoke(cli, ["browse-list", "--sort", "name"])
        lines = result.output.strip().split("\n")
        task_names = [line.split()[0] for line in lines[1:]]  # skip header
        assert task_names == ["alpha", "bravo", "charlie"]

    def test_browse_list_sort_modified(self, tmp_path, monkeypatch):
        """--sort modified should order by modification date (newest first)."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        # Create tasks with different modification times
        create_task(tasks_dir, "old-task", "active")
        time.sleep(0.05)
        create_task(tasks_dir, "new-task", "active")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        runner = CliRunner()
        result = runner.invoke(cli, ["browse-list", "--sort", "modified"])
        lines = result.output.strip().split("\n")
        task_names = [line.split()[0] for line in lines[1:]]  # skip header
        # Newest first
        assert task_names[0] == "new-task"
        assert task_names[1] == "old-task"


class TestGetBrowseLines:
    """Test the _get_browse_lines helper function directly."""

    def test_empty_dir(self, tmp_path):
        """Should return empty list for empty tasks dir."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        lines = _get_browse_lines(tasks_dir)
        assert lines == []

    def test_header_and_data_lines(self, tmp_path, monkeypatch):
        """First line should be header, followed by space-aligned data lines."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "my-task", "active", priority="high", project="proj")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        lines = _get_browse_lines(tasks_dir)
        assert len(lines) == 2  # header + 1 data line
        # Header row
        assert "NAME" in lines[0]
        assert "STATE" in lines[0]
        assert "PROJECT" in lines[0]
        assert "CREATED" in lines[0]
        # Data line: first field is task name
        assert lines[1].split()[0] == "my-task"

    def test_filter_defaults_to_backlog_active(self, tmp_path, monkeypatch):
        """Default filter should include current open-work states."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "t-active", "active")
        create_task(tasks_dir, "t-backlog", "backlog")
        create_task(tasks_dir, "t-todo", "todo")
        create_task(tasks_dir, "t-review", "ready_for_review")
        create_task(tasks_dir, "t-done", "done")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        lines = _get_browse_lines(tasks_dir)
        names = [line.split()[0] for line in lines[1:]]  # skip header
        assert "t-active" in names
        assert "t-backlog" in names
        assert "t-todo" in names
        assert "t-review" in names
        assert "t-done" not in names

    def test_show_all(self, tmp_path, monkeypatch):
        """show_all=True should include all tasks."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "t-active", "active")
        create_task(tasks_dir, "t-done", "done")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        lines = _get_browse_lines(tasks_dir, show_all=True)
        names = [line.split()[0] for line in lines[1:]]  # skip header
        assert "t-active" in names
        assert "t-done" in names


class TestBrowseFzfPreviewLabel:
    """Test that fzf preview has a label."""

    def test_browse_fzf_preview_label(self, tmp_path, monkeypatch):
        """fzf should have --preview-label set to Task Preview."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "my-task", "active")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        mock_result = MagicMock()
        mock_result.returncode = 130
        mock_result.stdout = ""

        with (
            patch("shutil.which", return_value="/usr/bin/fzf"),
            patch("subprocess.run", side_effect=_fzf_side_effect(mock_result)) as mock_run,
        ):
            runner = CliRunner()
            runner.invoke(cli, ["browse"])
            fzf_calls = _get_fzf_calls(mock_run)
            fzf_cmd = fzf_calls[0][0][0]
            label = _get_fzf_arg(fzf_cmd, "--preview-label")
            assert label is not None
            assert "Task Preview" in label

    def test_browse_fzf_blame_changes_preview_label(self, tmp_path, monkeypatch):
        """ctrl-b binding should include change-preview-label for Git Blame."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "my-task", "active")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        mock_result = MagicMock()
        mock_result.returncode = 130
        mock_result.stdout = ""

        with (
            patch("shutil.which", return_value="/usr/bin/fzf"),
            patch("subprocess.run", side_effect=_fzf_side_effect(mock_result)) as mock_run,
        ):
            runner = CliRunner()
            runner.invoke(cli, ["browse"])
            fzf_calls = _get_fzf_calls(mock_run)
            fzf_cmd = fzf_calls[0][0][0]
            bindings = _get_fzf_arg(fzf_cmd, "--bind")
            assert "change-preview-label( Git Blame )" in bindings

    def test_browse_fzf_log_changes_preview_label(self, tmp_path, monkeypatch):
        """ctrl-l binding should include change-preview-label for Git Log."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "my-task", "active")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        mock_result = MagicMock()
        mock_result.returncode = 130
        mock_result.stdout = ""

        with (
            patch("shutil.which", return_value="/usr/bin/fzf"),
            patch("subprocess.run", side_effect=_fzf_side_effect(mock_result)) as mock_run,
        ):
            runner = CliRunner()
            runner.invoke(cli, ["browse"])
            fzf_calls = _get_fzf_calls(mock_run)
            fzf_cmd = fzf_calls[0][0][0]
            bindings = _get_fzf_arg(fzf_cmd, "--bind")
            assert "change-preview-label( Git Log )" in bindings


class TestBrowseFzfRawBinding:
    """Test ctrl-r binding for raw markdown preview."""

    def test_browse_fzf_raw_binding(self, tmp_path, monkeypatch):
        """fzf --bind should include ctrl-r for raw markdown preview."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "my-task", "active")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        mock_result = MagicMock()
        mock_result.returncode = 130
        mock_result.stdout = ""

        with (
            patch("shutil.which", return_value="/usr/bin/fzf"),
            patch("subprocess.run", side_effect=_fzf_side_effect(mock_result)) as mock_run,
        ):
            runner = CliRunner()
            runner.invoke(cli, ["browse"])
            fzf_calls = _get_fzf_calls(mock_run)
            fzf_cmd = fzf_calls[0][0][0]
            bindings = _get_fzf_arg(fzf_cmd, "--bind")
            assert "ctrl-r:change-preview(gptodo show {1})" in bindings
            assert "change-preview-label( Raw Markdown )" in bindings


class TestShowRenderFlag:
    """Test the --render flag on the show command."""

    def test_show_render_flag(self, tmp_path, monkeypatch):
        """show --render should render markdown content."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "my-task", "active", content="# Hello\n\nSome **bold** text.")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        runner = CliRunner()
        result = runner.invoke(cli, ["show", "--render", "my-task"])
        assert result.exit_code == 0
        # Rich markdown renders bold differently — just check it doesn't crash
        # and the content appears
        assert "Hello" in result.output

    def test_show_raw_flag(self, tmp_path, monkeypatch):
        """show --raw should output raw markdown content."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "my-task", "active", content="# Hello\n\nSome **bold** text.")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        runner = CliRunner()
        result = runner.invoke(cli, ["show", "--raw", "my-task"])
        assert result.exit_code == 0
        assert "**bold**" in result.output

    def test_show_default_is_raw(self, tmp_path, monkeypatch):
        """show without --render should default to raw output."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "my-task", "active", content="Some **bold** text.")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        runner = CliRunner()
        result = runner.invoke(cli, ["show", "my-task"])
        assert result.exit_code == 0
        assert "**bold**" in result.output

    def test_show_named_task_uses_single_file_fast_path(self, tmp_path, monkeypatch):
        """show by task name should load only the selected file."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(
            tasks_dir,
            "fast-task",
            "active",
            created="2026-04-03",
            content="Fast path body.",
        )
        create_task(tasks_dir, "other-task", "active", created="2026-04-02")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        calls = []

        def tracked_load_tasks(tasks_dir_arg, recursive=False, single_file=None):
            calls.append(single_file)
            return load_tasks_util(tasks_dir_arg, recursive=recursive, single_file=single_file)

        with patch("gptodo.cli.load_tasks", side_effect=tracked_load_tasks):
            runner = CliRunner()
            result = runner.invoke(cli, ["show", "fast-task"])

        assert result.exit_code == 0
        assert "Fast path body." in result.output
        assert calls == [tasks_dir / "fast-task.md"]


class TestLoadTasksTimestampFallbacks:
    """Test fast timestamp fallbacks used by browse paths."""

    def test_load_tasks_avoids_git_when_modified_missing(self, tmp_path):
        """Missing modified should fall back to file mtime without git subprocesses."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "mtime-task", "active", created="2026-04-03")

        with patch("gptodo.utils.subprocess.run", side_effect=AssertionError("unexpected git")):
            tasks = load_tasks_util(tasks_dir)

        assert len(tasks) == 1
        assert tasks[0].name == "mtime-task"
        assert tasks[0].modified is not None


class TestBrowseFzfHeaderLines:
    """Test that fzf uses --header-lines for column headers."""

    def test_browse_fzf_header_lines(self, tmp_path, monkeypatch):
        """fzf should have --header-lines 1 to freeze column header."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "my-task", "active")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        mock_result = MagicMock()
        mock_result.returncode = 130
        mock_result.stdout = ""

        with (
            patch("shutil.which", return_value="/usr/bin/fzf"),
            patch("subprocess.run", side_effect=_fzf_side_effect(mock_result)) as mock_run,
        ):
            runner = CliRunner()
            runner.invoke(cli, ["browse"])
            fzf_calls = _get_fzf_calls(mock_run)
            fzf_cmd = fzf_calls[0][0][0]
            header_lines = _get_fzf_arg(fzf_cmd, "--header-lines")
            assert header_lines == "1"

    def test_browse_fzf_has_border(self, tmp_path, monkeypatch):
        """fzf should have --border for border-label to render on."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "my-task", "active")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        mock_result = MagicMock()
        mock_result.returncode = 130
        mock_result.stdout = ""

        with (
            patch("shutil.which", return_value="/usr/bin/fzf"),
            patch("subprocess.run", side_effect=_fzf_side_effect(mock_result)) as mock_run,
        ):
            runner = CliRunner()
            runner.invoke(cli, ["browse"])
            fzf_calls = _get_fzf_calls(mock_run)
            fzf_cmd = fzf_calls[0][0][0]
            assert "--border" in fzf_cmd

    def test_browse_fzf_layout_reverse(self, tmp_path, monkeypatch):
        """fzf should use --layout reverse so column headers appear at top."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "my-task", "active")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        mock_result = MagicMock()
        mock_result.returncode = 130
        mock_result.stdout = ""

        with (
            patch("shutil.which", return_value="/usr/bin/fzf"),
            patch("subprocess.run", side_effect=_fzf_side_effect(mock_result)) as mock_run,
        ):
            runner = CliRunner()
            runner.invoke(cli, ["browse"])
            fzf_calls = _get_fzf_calls(mock_run)
            fzf_cmd = fzf_calls[0][0][0]
            layout = _get_fzf_arg(fzf_cmd, "--layout")
            assert layout == "reverse"

    def test_browse_fzf_rendered_preview(self, tmp_path, monkeypatch):
        """fzf --preview should use gptodo show --render."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        create_task(tasks_dir, "my-task", "active")
        monkeypatch.setenv("GPTODO_TASKS_DIR", str(tasks_dir))

        mock_result = MagicMock()
        mock_result.returncode = 130
        mock_result.stdout = ""

        with (
            patch("shutil.which", return_value="/usr/bin/fzf"),
            patch("subprocess.run", side_effect=_fzf_side_effect(mock_result)) as mock_run,
        ):
            runner = CliRunner()
            runner.invoke(cli, ["browse"])
            fzf_calls = _get_fzf_calls(mock_run)
            fzf_cmd = fzf_calls[0][0][0]
            preview = _get_fzf_arg(fzf_cmd, "--preview")
            assert "gptodo show --render" in preview
