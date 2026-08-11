"""Tests for count_subtasks — including intentionally-skipped checkboxes.

The three skipped forms and, critically, the denominator rule: skipped items
stay in ``total`` so they can never launder a partial closure into 100%.
"""

import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from gptodo.checker import check_subtask_completion
from gptodo.cli import cli
from gptodo.utils import SubtaskCount, count_subtasks


class TestPlainCheckboxes:
    """Pre-existing behavior must be unchanged."""

    def test_no_checkboxes(self):
        assert count_subtasks("Just some prose.\n") == SubtaskCount(0, 0, 0)

    def test_done_and_pending(self):
        counts = count_subtasks("- [x] one\n- [x] two\n- [ ] three\n")
        assert (counts.completed, counts.total, counts.skipped) == (2, 3, 0)
        assert counts.pending == 1

    def test_emoji_forms(self):
        counts = count_subtasks("- ✅ shipped\n- 🏃 in progress\n")
        assert (counts.completed, counts.total, counts.skipped) == (1, 2, 0)

    def test_str_without_skipped_is_unchanged(self):
        assert str(count_subtasks("- [x] a\n- [ ] b\n")) == "(1/2)"
        assert str(count_subtasks("no boxes")) == ""


class TestSkippedCheckboxes:
    @pytest.mark.parametrize(
        "line",
        [
            "- [-] Wire GitHub issue indexing (deferred: cache mutates under you)",
            "- [ ] ~~Wire GitHub issue indexing~~ (deferred: spec'd separately)",
            "- [x] ~~Wire GitHub issue indexing~~ (decided against: superseded)",
        ],
    )
    def test_all_three_forms_count_as_skipped(self, line):
        counts = count_subtasks(line + "\n")
        assert (counts.completed, counts.total, counts.skipped) == (0, 1, 1)

    def test_skipped_boxes_do_not_inflate_completion(self):
        """The laundering guard: 5 criteria, 2 done, 3 skipped is 40%, not 100%.

        Ignoring the marker would drop skipped items from both numerator and
        denominator, reporting 2/2 = 100% — so an author could mark the hard
        criteria skipped and sail through the done-gate.
        """
        content = (
            "- [x] easy one\n"
            "- [x] easy two\n"
            "- [-] hard one (deferred: needs upstream fix)\n"
            "- [ ] ~~hard two~~ (deferred: spec'd separately)\n"
            "- [x] ~~hard three~~ (decided against: superseded)\n"
        )
        counts = count_subtasks(content)
        assert (counts.completed, counts.skipped, counts.pending) == (2, 3, 0)
        assert counts.total == 5, "skipped items must remain in the denominator"
        assert counts.completed / counts.total == pytest.approx(0.4)
        assert counts.completed / counts.total != 1.0

    def test_strikethrough_is_not_double_counted(self):
        counts = count_subtasks("- [x] ~~a~~ (skipped: X)\n- [ ] ~~b~~ (skipped: Y)\n")
        assert (counts.completed, counts.total, counts.skipped) == (0, 2, 2)

    def test_mid_text_strikethrough_is_not_a_skip(self):
        counts = count_subtasks("- [ ] Fix the ~~old~~ new thing\n")
        assert (counts.completed, counts.total, counts.skipped) == (0, 1, 0)

    def test_skip_marker_word_form_is_not_supported(self):
        """`- [SKIP]` was documented but never implemented; it stays that way."""
        assert count_subtasks("- [SKIP] Content schedule established\n") == SubtaskCount(0, 0, 0)

    def test_str_surfaces_skipped(self):
        assert str(count_subtasks("- [x] a\n- [-] b (skipped: X)\n")) == "(1/2, 1 skipped)"


class TestCheckerDoesNotPassOnSkipped:
    def _task(self, subtasks, state="done"):
        class _T:
            pass

        t = _T()
        t.subtasks = subtasks
        t.state = state
        return t

    def test_done_task_with_skipped_criteria_fails_the_gate(self):
        """2 done + 3 skipped must NOT read as complete for a done task."""
        counts = count_subtasks(
            "- [x] a\n"
            "- [x] b\n"
            "- [-] c (deferred: upstream)\n"
            "- [ ] ~~d~~ (deferred: separate spec)\n"
            "- [x] ~~e~~ (decided against: superseded)\n"
        )
        result = check_subtask_completion(self._task(counts))
        assert result["passed"] is False
        assert result["details"]["total"] == 5
        assert result["details"]["skipped"] == 3
        assert "intentionally skipped" in result["message"]

    def test_fully_done_task_still_passes(self):
        counts = count_subtasks("- [x] a\n- [x] b\n")
        result = check_subtask_completion(self._task(counts))
        assert result["passed"] is True


class TestSetSubtaskToggleSkipped:
    """--set-subtask must de-strikethrough struck forms when toggling back to done/todo.

    Regression for the P1 bug where toggling `- [ ] ~~text~~ (reason)` to done
    produced `- [x] ~~text~~ (reason)`, which count_subtasks re-classifies as
    skipped, making the toggle a silent no-op.
    """

    TASK_TEMPLATE = textwrap.dedent("""\
        ---
        state: active
        created: 2026-01-01T00:00:00+00:00
        ---
        # Test task

        {line}
        """)

    def _run_toggle(self, tmp_path: Path, monkeypatch, line: str, state: str) -> str:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        task_file = tasks_dir / "test-toggle.md"
        task_file.write_text(self.TASK_TEMPLATE.format(line=line))
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["edit", "test-toggle", "--set-subtask", "Wire GitHub issue indexing", state],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        return task_file.read_text()

    @pytest.mark.parametrize(
        "skipped_line",
        [
            "- [ ] ~~Wire GitHub issue indexing~~ (deferred: spec'd separately)",
            "- [x] ~~Wire GitHub issue indexing~~ (decided against: superseded)",
        ],
    )
    def test_struck_form_toggle_to_done_produces_plain_checkbox(
        self, tmp_path: Path, monkeypatch, skipped_line: str
    ):
        content = self._run_toggle(tmp_path, monkeypatch, skipped_line, "done")
        assert "- [x] Wire GitHub issue indexing" in content
        # The struck form must be gone — if it stays the item is still skipped
        assert "~~Wire GitHub issue indexing~~" not in content
        counts = count_subtasks(content)
        assert counts.skipped == 0
        assert counts.completed == 1

    @pytest.mark.parametrize(
        "skipped_line",
        [
            "- [ ] ~~Wire GitHub issue indexing~~ (deferred: spec'd separately)",
            "- [x] ~~Wire GitHub issue indexing~~ (decided against: superseded)",
        ],
    )
    def test_struck_form_toggle_to_todo_produces_plain_checkbox(
        self, tmp_path: Path, monkeypatch, skipped_line: str
    ):
        content = self._run_toggle(tmp_path, monkeypatch, skipped_line, "todo")
        assert "- [ ] Wire GitHub issue indexing" in content
        assert "~~Wire GitHub issue indexing~~" not in content
        counts = count_subtasks(content)
        assert counts.skipped == 0
        assert counts.pending == 1

    @pytest.mark.parametrize(
        "skipped_line",
        [
            "  - [ ] ~~Wire GitHub issue indexing~~ (deferred: spec'd separately)",
            "  - [x] ~~Wire GitHub issue indexing~~ (decided against: superseded)",
        ],
    )
    def test_indented_struck_form_toggle_strips_strikethrough(
        self, tmp_path: Path, monkeypatch, skipped_line: str
    ):
        """Indented checkboxes must de-strike correctly — the regex was anchored at ^
        which skipped any leading whitespace, leaving ~~markup~~ intact and causing
        count_subtasks to re-classify the toggled item as skipped (silent no-op).
        """
        content = self._run_toggle(tmp_path, monkeypatch, skipped_line, "done")
        assert "~~Wire GitHub issue indexing~~" not in content
        counts = count_subtasks(content)
        assert counts.skipped == 0
        assert counts.completed == 1

    @pytest.mark.parametrize(
        "skipped_line",
        [
            "- [-] Wire GitHub issue indexing (deferred: cache mutates under you)",
            "- [-] Wire GitHub issue indexing (decided against: superseded)",
        ],
    )
    def test_bare_skip_form_toggle_to_done_strips_reason(
        self, tmp_path: Path, monkeypatch, skipped_line: str
    ):
        """Toggling bare [-] to done must strip the trailing reason parenthetical.

        Without this, '- [x] Wire ... (deferred: X)' would remain — stale
        annotation on a completed item, inconsistent with the strikethrough forms.
        """
        content = self._run_toggle(tmp_path, monkeypatch, skipped_line, "done")
        assert "- [x] Wire GitHub issue indexing" in content
        assert "(deferred:" not in content
        assert "(decided against:" not in content
        counts = count_subtasks(content)
        assert counts.skipped == 0
        assert counts.completed == 1

    @pytest.mark.parametrize(
        "skipped_line",
        [
            "- [-] Wire GitHub issue indexing (deferred: cache mutates under you)",
        ],
    )
    def test_bare_skip_form_toggle_to_todo_strips_reason(
        self, tmp_path: Path, monkeypatch, skipped_line: str
    ):
        """Toggling bare [-] to todo must also strip the trailing reason."""
        content = self._run_toggle(tmp_path, monkeypatch, skipped_line, "todo")
        assert "- [ ] Wire GitHub issue indexing" in content
        assert "(deferred:" not in content
        counts = count_subtasks(content)
        assert counts.skipped == 0
        assert counts.pending == 1
