"""Tests for AgentEmail._parse_headers empty-value and folded-header handling."""

from pathlib import Path

import pytest

from gptmail.lib import AgentEmail


@pytest.fixture
def agent(tmp_path: Path) -> AgentEmail:
    email_dir = tmp_path / "email"
    for subdir in ["inbox", "sent", "archive", "drafts", "filters"]:
        (email_dir / subdir).mkdir(parents=True, exist_ok=True)
    return AgentEmail(str(tmp_path), "test@example.com")


def test_normal_header(agent: AgentEmail):
    headers = agent._parse_headers("Subject: Hello World")
    assert headers == {"Subject": "Hello World"}


def test_empty_value_github_headers(agent: AgentEmail):
    """GitHub notification emails send `X-GitHub-Labels:` with no value."""
    raw = (
        "Subject: [gptme] mention\n"
        "X-GitHub-Labels:\n"
        "X-GitHub-Assignees:\n"
        "Date: Mon, 06 Oct 2026 15:30:00 +0000"
    )
    headers = agent._parse_headers(raw)
    assert headers["Subject"] == "[gptme] mention"
    assert headers["X-GitHub-Labels"] == ""
    assert headers["X-GitHub-Assignees"] == ""
    assert headers["Date"] == "Mon, 06 Oct 2026 15:30:00 +0000"


def test_colon_in_value_preserved(agent: AgentEmail):
    headers = agent._parse_headers("Date: Mon, 06 Oct 2026 15:30:00 +0000")
    assert headers["Date"] == "Mon, 06 Oct 2026 15:30:00 +0000"


def test_folded_continuation_after_empty_value_has_no_leading_space(
    agent: AgentEmail,
):
    """Bare `Key:` plus an indented continuation must not join as ` <value>`."""
    raw = "References:\n <a@b>"
    headers = agent._parse_headers(raw)
    assert headers["References"] == "<a@b>"


def test_folded_continuation_after_value(agent: AgentEmail):
    raw = "Subject: Hello\n World"
    headers = agent._parse_headers(raw)
    assert headers["Subject"] == "Hello World"


def test_invalid_header_line_goes_to_stderr(agent: AgentEmail, capsys):
    headers = agent._parse_headers("Subject: ok\nno colon here")
    assert headers == {"Subject": "ok"}
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Warning: Invalid header line: no colon here" in captured.err


def test_invalid_line_does_not_clobber_empty_value_header(agent: AgentEmail, capsys):
    headers = agent._parse_headers("X-GitHub-Labels:\nno colon here")
    assert headers == {"X-GitHub-Labels": ""}
    assert "Warning: Invalid header line: no colon here" in capsys.readouterr().err
