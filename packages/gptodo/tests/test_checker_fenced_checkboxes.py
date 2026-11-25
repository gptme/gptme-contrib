"""gptme/gptme-contrib#2304 item 9: the checker must count checkboxes the way
the CLI done gate does, ignoring ``- [ ]`` inside fenced code examples."""

from __future__ import annotations

from pathlib import Path

from gptodo.checker import check_subtask_completion
from gptodo.utils import load_tasks

BODY = """# Done task

- [x] Ship the fix

Example of the old format:

```markdown
- [ ] an example checkbox, not a subtask
```
"""


def _task(tmp_path: Path, body: str):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "t.md").write_text(
        "---\nstate: done\ncreated: 2026-11-01T00:00:00+00:00\n---\n" + body
    )
    (task,) = load_tasks(tasks_dir)
    return task


def test_fenced_example_checkbox_does_not_fail_a_done_task(tmp_path: Path) -> None:
    result = check_subtask_completion(_task(tmp_path, BODY))
    assert result["passed"], result["message"]


def test_real_pending_checkbox_still_fails(tmp_path: Path) -> None:
    result = check_subtask_completion(_task(tmp_path, BODY + "\n- [ ] Real pending item\n"))
    assert not result["passed"]
