"""Tests for state-transition legality enforcement in `gptodo edit`.

Gordon 2026-07-01 upstream fix: the autonomous loop drifted watch-* tasks
into `active` when they should have been `waiting`; when they were corrected
they should go `active → waiting`, not `active → todo`. This exercise
verifies:

  1. Legal transitions (e.g. todo → active) work silently.
  2. Terminal-state reopens (done/cancelled → anything) are BLOCKED without
     --force (terminal states are sticky by design).
  3. Non-terminal illegal transitions (e.g. active → todo) WARN by default
     but still succeed — real workflows use enough shortcuts that a hard
     block would break more than it fixes.
  4. GPTODO_STRICT_TRANSITIONS=1 promotes non-terminal illegal to BLOCK.
  5. --force bypasses both terminal-reopen block and strict-mode block.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.utils import load_tasks


def _write(tasks_dir: Path, name: str, state: str) -> None:
    (tasks_dir / f"{name}.md").write_text(
        f"---\nstate: {state}\ncreated: 2026-06-01T00:00:00+00:00\n---\n# {name}\n"
    )


def test_legal_transition_succeeds_silently(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "t", "todo")

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["edit", "t", "--set", "state", "active"])

    assert result.exit_code == 0, result.output
    assert "illegal" not in result.output.lower()
    assert "warning" not in result.output.lower()
    tasks = load_tasks(tasks_dir)
    assert tasks[0].metadata["state"] == "active"


def test_terminal_state_reopen_blocked_without_force(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "closed", "done")

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["edit", "closed", "--set", "state", "active"])

    # Should refuse and leave state as done
    assert result.exit_code == 1, result.output
    assert "Refusing to reopen terminal state" in result.output
    tasks = load_tasks(tasks_dir)
    assert tasks[0].metadata["state"] == "done"


def test_terminal_state_reopen_allowed_with_force(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "closed", "cancelled")

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["edit", "closed", "--set", "state", "backlog", "--force"])

    assert result.exit_code == 0, result.output
    assert "--force" in result.output
    tasks = load_tasks(tasks_dir)
    assert tasks[0].metadata["state"] == "backlog"


def test_illegal_nonterminal_transition_warns_but_succeeds(tmp_path: Path, monkeypatch) -> None:
    """active → todo is illegal per VALID_TRANSITIONS but common in practice.

    Default behavior: warn, don't block. This is the specific drift the
    Gordon session flagged (watch-* tasks stuck in `active` — the correction
    should be `active → waiting`, not `active → todo`).
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "watch-thing", "active")

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["edit", "watch-thing", "--set", "state", "todo"])

    assert result.exit_code == 0, result.output
    assert "illegal state transition" in result.output.lower()
    # Legal transitions from active should be hinted
    assert "Legal from 'active'" in result.output
    # But the edit still applies
    tasks = load_tasks(tasks_dir)
    assert tasks[0].metadata["state"] == "todo"


def test_strict_mode_env_var_blocks_illegal_transition(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "watch-thing", "active")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GPTODO_STRICT_TRANSITIONS", "1")
    result = CliRunner().invoke(cli, ["edit", "watch-thing", "--set", "state", "todo"])

    assert result.exit_code == 1, result.output
    assert "GPTODO_STRICT_TRANSITIONS" in result.output
    # Not applied
    tasks = load_tasks(tasks_dir)
    assert tasks[0].metadata["state"] == "active"


def test_strict_mode_bypassed_by_force(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "watch-thing", "active")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GPTODO_STRICT_TRANSITIONS", "1")
    result = CliRunner().invoke(cli, ["edit", "watch-thing", "--set", "state", "todo", "--force"])

    assert result.exit_code == 0, result.output
    tasks = load_tasks(tasks_dir)
    assert tasks[0].metadata["state"] == "todo"


def test_active_to_waiting_is_legal(tmp_path: Path, monkeypatch) -> None:
    """The specific correction path Gordon needs for watch-* task drift."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "watch-thing", "active")

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "edit",
            "watch-thing",
            "--set",
            "state",
            "waiting",
            "--set",
            "waiting_for",
            "some-gate-firing",
        ],
    )

    assert result.exit_code == 0, result.output
    # No warning printed for a legal transition
    assert "illegal" not in result.output.lower()
    tasks = load_tasks(tasks_dir)
    assert tasks[0].metadata["state"] == "waiting"


def test_noop_state_change_does_not_warn(tmp_path: Path, monkeypatch) -> None:
    """Editing state=active on an already-active task should not warn."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "t", "active")

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["edit", "t", "--set", "state", "active"])

    assert result.exit_code == 0, result.output
    assert "illegal" not in result.output.lower()
