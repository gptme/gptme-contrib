"""Tests for tracker-backed completion status CLI commands."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import gptmail.cli as gptmail_cli
from gptmail.lib import AgentEmail


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a minimal workspace for CLI completion tests."""
    email_dir = tmp_path / "email"
    for subdir in ["inbox", "sent", "archive", "drafts", "filters"]:
        (email_dir / subdir).mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("EMAIL_ALLOWLIST", "friend@example.com")
    monkeypatch.setattr(gptmail_cli, "get_workspace_dir", lambda: tmp_path)
    return tmp_path


def test_list_completed_and_status_show_no_reply_reason(workspace: Path) -> None:
    agent = AgentEmail(str(workspace), "test@example.com")
    agent._mark_no_reply_needed("<thread@example.com>", "Informational only")

    runner = CliRunner()

    result = runner.invoke(gptmail_cli.cli, ["list-completed"])
    assert result.exit_code == 0
    assert "Found 1 completed emails:" in result.output
    assert "no_reply_needed" in result.output
    assert "Reason: Informational only" in result.output
    assert "thread@example.com" in result.output

    status_result = runner.invoke(
        gptmail_cli.cli, ["check-completion-status", "<thread@example.com>"]
    )
    assert status_result.exit_code == 0
    assert "Is completed: True" in status_result.output
    assert "thread@example.com" in status_result.output
    assert "Informational only" in status_result.output


def test_list_completed_maps_completed_state_to_replied(workspace: Path) -> None:
    agent = AgentEmail(str(workspace), "test@example.com")
    agent._mark_replied("<thread@example.com>", "<reply@example.com>")

    runner = CliRunner()
    result = runner.invoke(gptmail_cli.cli, ["list-completed", "--status", "replied"])

    assert result.exit_code == 0
    assert "Found 1 completed emails:" in result.output
    assert "replied" in result.output
    assert "Reply ID: <reply@example.com>" in result.output
    assert "thread@example.com" in result.output


def test_list_completed_tolerates_legacy_extra_tracker_fields(workspace: Path) -> None:
    agent = AgentEmail(str(workspace), "test@example.com")
    agent._mark_no_reply_needed("<legacy@example.com>", "Legacy entry")

    state_file = workspace / "email" / "locks" / "email.json"
    state_data = json.loads(state_file.read_text())
    state_data["messages"]["legacy@example.com"]["platform"] = "email"
    state_data["messages"]["legacy@example.com"]["status"] = "no_reply_needed"
    state_file.write_text(json.dumps(state_data, indent=2))

    runner = CliRunner()
    result = runner.invoke(gptmail_cli.cli, ["list-completed"])

    assert result.exit_code == 0
    assert "legacy@example.com" in result.output
    assert "Reason: Legacy entry" in result.output
