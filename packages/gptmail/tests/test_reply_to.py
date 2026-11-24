"""Tests for Reply-To handling in maildir sync and ``gptmail reply``."""

from pathlib import Path

import pytest
from click.testing import CliRunner

import gptmail.cli as gptmail_cli
from gptmail.lib import AgentEmail

from .test_unreplied_emails import _write_maildir_email


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgentEmail:
    email_dir = tmp_path / "email"
    for subdir in ["inbox", "sent", "archive", "drafts", "filters"]:
        (email_dir / subdir).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EMAIL_ALLOWLIST", "friend@example.com")
    return AgentEmail(str(tmp_path), "test@example.com")


def test_maildir_sync_preserves_reply_to(agent: AgentEmail, tmp_path: Path) -> None:
    maildir = tmp_path / "maildir"
    (maildir / "new").mkdir(parents=True)
    message_id = "<reply-to-sync@example.com>"
    _write_maildir_email(
        maildir / "new" / "reply-to-sync",
        message_id=message_id,
        subject="Web form",
        to="Bob <test@example.com>",
        from_header="Forms <noreply@forms.example.com>",
        extra_headers=["Reply-To: Visitor <visitor@example.com>"],
    )
    agent.external_maildir_folders["inbox"] = maildir

    agent.sync_from_maildir("inbox")

    path = agent.email_dir / "inbox" / agent._format_filename(message_id)
    headers, _ = agent._markdown_to_email(path.read_text())
    assert headers["Reply-To"] == "Visitor <visitor@example.com>"


def _reply_draft_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, reply_to: str, send_allowlist: str
) -> tuple[str, str]:
    email_dir = tmp_path / "email"
    for subdir in ["inbox", "sent", "archive", "drafts", "filters"]:
        (email_dir / subdir).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EMAIL_ALLOWLIST", "friend@example.com")
    monkeypatch.setenv("EMAIL_SEND_ALLOWLIST", send_allowlist)
    monkeypatch.setenv("AGENT_EMAIL", "test@example.com")
    monkeypatch.setattr(gptmail_cli, "get_workspace_dir", lambda: tmp_path)

    message_id = "<reply-to-cli@example.com>"
    agent = AgentEmail(str(tmp_path), "test@example.com")
    (email_dir / "inbox" / agent._format_filename(message_id)).write_text(
        "\n".join(
            [
                "From: Friend <friend@example.com>",
                "To: Bob <test@example.com>",
                f"Reply-To: {reply_to}",
                "Date: Tue, 08 Sep 2026 16:00:00 +0000",
                "Subject: Relay",
                f"Message-ID: {message_id}",
                "",
                "Please answer the list.",
            ]
        )
    )

    result = CliRunner().invoke(gptmail_cli.cli, ["reply", message_id, "Answer."])
    assert result.exit_code == 0, result.output
    drafts = list((email_dir / "drafts").glob("*.md"))
    assert len(drafts) == 1
    draft_headers = drafts[0].read_text().split("\n\n", 1)[0]
    to_lines = [line for line in draft_headers.splitlines() if line.startswith("To:")]
    assert len(to_lines) == 1
    return to_lines[0], result.output


def test_reply_answers_allowlisted_reply_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    to_line, _ = _reply_draft_to(
        tmp_path,
        monkeypatch,
        reply_to="List <list@example.com>",
        send_allowlist="friend@example.com,list@example.com",
    )
    assert to_line == "To: List <list@example.com>"


def test_reply_ignores_reply_to_outside_send_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forged From plus an attacker's Reply-To must not redirect the reply."""
    to_line, output = _reply_draft_to(
        tmp_path,
        monkeypatch,
        reply_to="list@example.com, attacker@evil.example",
        send_allowlist="friend@example.com,list@example.com",
    )
    assert to_line == "To: Friend <friend@example.com>"
    assert "Reply-To" in output
