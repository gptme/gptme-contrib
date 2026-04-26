"""Tests for the ``gptodo claim`` command."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.frontmatter_compat import frontmatter


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Workspace with a tasks/ dir and a marker file ``find_repo_root`` will pick up."""
    (tmp_path / "tasks").mkdir()
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "Bob"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GPTODO_AGENT_NAME", raising=False)
    return tmp_path


def write_task(workspace: Path, name: str, **metadata: object) -> Path:
    """Write a task file with frontmatter and return its path."""
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


def load_meta(path: Path) -> dict:
    return dict(frontmatter.load(path).metadata)


def test_claim_backlog_task_promotes_to_active(workspace: Path) -> None:
    path = write_task(
        workspace, "backlog-task", state="backlog", created="2026-04-26T00:00:00+00:00"
    )

    result = CliRunner().invoke(cli, ["claim", "backlog-task", "--agent", "bob"])

    assert result.exit_code == 0, result.output
    meta = load_meta(path)
    assert meta["state"] == "active"
    assert meta["assigned_to"] == "bob"
    assert isinstance(meta["assigned_at"], str)
    assert meta["assigned_at"].endswith("+00:00") or "T" in meta["assigned_at"]


def test_claim_todo_task_promotes_to_active(workspace: Path) -> None:
    path = write_task(workspace, "todo-task", state="todo", created="2026-04-26T00:00:00+00:00")

    result = CliRunner().invoke(cli, ["claim", "todo-task", "--agent", "bob"])

    assert result.exit_code == 0
    meta = load_meta(path)
    assert meta["state"] == "active"
    assert meta["assigned_to"] == "bob"
    assert "assigned_at" in meta


def test_claim_active_already_owned_is_noop(workspace: Path) -> None:
    """Claiming an already-claimed active task by the same owner preserves assigned_at."""
    original_assigned_at = "2026-04-25T12:00:00+00:00"
    path = write_task(
        workspace,
        "active-task",
        state="active",
        created="2026-04-26T00:00:00+00:00",
        assigned_to="bob",
        assigned_at=original_assigned_at,
    )

    result = CliRunner().invoke(cli, ["claim", "active-task", "--agent", "bob"])

    assert result.exit_code == 0
    assert "Already claimed" in result.output
    meta = load_meta(path)
    assert meta["state"] == "active"
    assert meta["assigned_to"] == "bob"
    # First-claim timestamp preserved (YAML parses ISO datetimes; str uses " " separator).
    preserved = str(meta["assigned_at"])
    assert preserved.startswith("2026-04-25") and "12:00:00" in preserved


def test_claim_active_reassigns_owner(workspace: Path) -> None:
    """Reassignment updates owner and bumps assigned_at."""
    path = write_task(
        workspace,
        "active-task",
        state="active",
        created="2026-04-26T00:00:00+00:00",
        assigned_to="bob",
        assigned_at="2026-04-25T12:00:00+00:00",
    )

    result = CliRunner().invoke(cli, ["claim", "active-task", "--agent", "erik"])

    assert result.exit_code == 0
    meta = load_meta(path)
    assert meta["state"] == "active"
    assert meta["assigned_to"] == "erik"
    # Timestamp was bumped (no longer the original).
    bumped = str(meta["assigned_at"])
    assert not (bumped.startswith("2026-04-25") and "12:00:00" in bumped)


def test_claim_waiting_task_refuses(workspace: Path) -> None:
    path = write_task(
        workspace,
        "waiting-task",
        state="waiting",
        created="2026-04-26T00:00:00+00:00",
        waiting_for="Erik review",
    )

    result = CliRunner().invoke(cli, ["claim", "waiting-task", "--agent", "bob"])

    assert result.exit_code != 0
    assert "Refusing to claim" in result.output
    # Metadata unchanged.
    meta = load_meta(path)
    assert meta["state"] == "waiting"
    assert "assigned_to" not in meta
    assert "assigned_at" not in meta


@pytest.mark.parametrize("state", ["done", "cancelled", "ready_for_review", "someday"])
def test_claim_terminal_or_deferred_refuses(workspace: Path, state: str) -> None:
    path = write_task(workspace, f"{state}-task", state=state, created="2026-04-26T00:00:00+00:00")

    result = CliRunner().invoke(cli, ["claim", f"{state}-task"])

    assert result.exit_code != 0
    meta = load_meta(path)
    assert meta["state"] == state


def test_claim_resolves_agent_from_env(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GPTODO_AGENT_NAME", "alice")
    path = write_task(workspace, "env-task", state="backlog", created="2026-04-26T00:00:00+00:00")

    result = CliRunner().invoke(cli, ["claim", "env-task"])

    assert result.exit_code == 0
    meta = load_meta(path)
    assert meta["assigned_to"] == "alice"


def test_claim_ignores_blank_agent_override(workspace: Path) -> None:
    path = write_task(
        workspace, "blank-override-task", state="backlog", created="2026-04-26T00:00:00+00:00"
    )

    result = CliRunner().invoke(cli, ["claim", "blank-override-task", "--agent", "   "])

    assert result.exit_code == 0, result.output
    meta = load_meta(path)
    assert meta["assigned_to"] == "bob"


def test_claim_ignores_blank_env(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GPTODO_AGENT_NAME", "   ")
    path = write_task(
        workspace, "blank-env-task", state="backlog", created="2026-04-26T00:00:00+00:00"
    )

    result = CliRunner().invoke(cli, ["claim", "blank-env-task"])

    assert result.exit_code == 0, result.output
    meta = load_meta(path)
    assert meta["assigned_to"] == "bob"


def test_claim_resolves_agent_from_gptme_toml(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gptme.toml has [agent] name = "Bob"; expect lowercased "bob" when no override/env."""
    monkeypatch.delenv("GPTODO_AGENT_NAME", raising=False)
    path = write_task(workspace, "toml-task", state="backlog", created="2026-04-26T00:00:00+00:00")

    result = CliRunner().invoke(cli, ["claim", "toml-task"])

    assert result.exit_code == 0
    meta = load_meta(path)
    assert meta["assigned_to"] == "bob"


def test_claim_warns_on_invalid_gptme_toml(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GPTODO_AGENT_NAME", raising=False)
    (workspace / "gptme.toml").write_text("[agent\nname = Bob\n")
    path = write_task(
        workspace, "invalid-toml-task", state="backlog", created="2026-04-26T00:00:00+00:00"
    )

    result = CliRunner().invoke(cli, ["claim", "invalid-toml-task"])

    assert result.exit_code == 0, result.output
    assert "Warning: Failed to load" in result.output
    meta = load_meta(path)
    assert meta["assigned_to"] == "agent"


def test_edit_accepts_named_assigned_to(workspace: Path) -> None:
    """Regression: ``edit --set assigned_to bob`` must not be rejected as enum-invalid."""
    path = write_task(workspace, "named-task", state="backlog", created="2026-04-26T00:00:00+00:00")

    result = CliRunner().invoke(cli, ["edit", "named-task", "--set", "assigned_to", "bob"])

    assert result.exit_code == 0, result.output
    meta = load_meta(path)
    assert meta["assigned_to"] == "bob"


def test_edit_clears_assigned_to_with_none(workspace: Path) -> None:
    path = write_task(
        workspace,
        "named-task",
        state="backlog",
        created="2026-04-26T00:00:00+00:00",
        assigned_to="bob",
    )

    result = CliRunner().invoke(cli, ["edit", "named-task", "--set", "assigned_to", "none"])

    assert result.exit_code == 0, result.output
    meta = load_meta(path)
    assert "assigned_to" not in meta


def test_claim_unknown_task_fails(workspace: Path) -> None:
    write_task(workspace, "real-task", state="backlog", created="2026-04-26T00:00:00+00:00")

    result = CliRunner().invoke(cli, ["claim", "nonexistent-task"])

    assert result.exit_code != 0


# Sanity check: GPTODO_AGENT_NAME must not leak from the test runner's environment.
def test_runner_env_does_not_leak(workspace: Path) -> None:
    assert "GPTODO_AGENT_NAME" not in os.environ
