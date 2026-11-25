"""gptme/gptme-contrib#2304 items 4 and 5.

4: an unknown ``type:`` must not crash; the condition stays unresolved and is
   written back unchanged.
5: clearing resolved conditions must keep unresolved ``type: task`` entries.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.frontmatter_compat import frontmatter
from gptodo.waiting import (
    WaitCondition,
    WaitType,
    check_condition,
    check_task_waiting,
    parse_waiting_for,
)

PAST = "2020-01-01T00:00:00+00:00"


def _write(path: Path, waiting_for: str) -> Path:
    path.write_text(
        "---\nstate: waiting\ncreated: 2026-11-01T00:00:00+00:00\n"
        f"waiting_since: '2026-11-02T00:00:00+00:00'\nwaiting_for:\n{waiting_for}---\n# t\n"
    )
    return path


def test_unknown_type_does_not_crash_and_never_resolves() -> None:
    cond = WaitCondition.from_dict({"type": "pr-merged", "ref": "o/r#1"})
    assert cond.type is WaitType.UNKNOWN
    checked = check_condition(cond)
    assert not checked.resolved
    assert checked.error and "pr-merged" in checked.error
    assert cond.to_dict() == {"type": "pr-merged", "ref": "o/r#1"}


def test_unknown_type_blocks_fix(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "t.md",
        f"  - type: time\n    ref: '{PAST}'\n  - type: pr-merged\n    ref: o/r#1\n",
    )
    before = path.read_text()
    resolved, _ = check_task_waiting(path)
    assert not resolved
    assert path.read_text() == before


def test_fix_keeps_unresolved_task_entries(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "t.md",
        f"  - type: time\n    ref: '{PAST}'\n  - type: task\n    ref: other-task\n",
    )
    resolved, _ = check_task_waiting(path)
    assert resolved
    meta = frontmatter.load(path).metadata
    assert [c.ref for c in parse_waiting_for(meta)] == ["other-task"]
    assert meta.get("waiting_since")


def test_fix_clears_when_only_external_conditions(tmp_path: Path) -> None:
    path = _write(tmp_path / "t.md", f"  - type: time\n    ref: '{PAST}'\n")
    resolved, _ = check_task_waiting(path)
    assert resolved
    meta = frontmatter.load(path).metadata
    assert "waiting_for" not in meta and "waiting_since" not in meta


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "tasks").mkdir()
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "Bob"\n')
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_cli_check_waiting_fix_keeps_task_entries(workspace: Path) -> None:
    path = _write(
        workspace / "tasks" / "t.md",
        f"  - type: time\n    ref: '{PAST}'\n  - type: task\n    ref: other-task\n",
    )
    result = CliRunner().invoke(cli, ["check-waiting", "--fix"])
    assert result.exit_code == 0, result.output
    meta = frontmatter.load(path).metadata
    assert [c.ref for c in parse_waiting_for(meta)] == ["other-task"]


def test_cli_check_waiting_survives_unknown_type(workspace: Path) -> None:
    _write(workspace / "tasks" / "t.md", "  - type: pr-merged\n    ref: o/r#1\n")
    result = CliRunner().invoke(cli, ["check-waiting"])
    assert result.exit_code == 0, result.output
