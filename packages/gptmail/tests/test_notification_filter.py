"""Tests for the notification email filter in gptmail."""

from pathlib import Path

import pytest

from gptmail.lib import AgentEmail


@pytest.fixture
def agent(tmp_path: Path) -> AgentEmail:
    """Create a minimal AgentEmail with required directory structure."""
    email_dir = tmp_path / "email"
    for subdir in ["inbox", "sent", "archive", "drafts", "filters"]:
        (email_dir / subdir).mkdir(parents=True, exist_ok=True)
    return AgentEmail(str(tmp_path), "test@example.com")


def test_subject_match(agent: AgentEmail):
    """Notification patterns in subject are detected."""
    assert agent._is_notification_email("Security Alert for your account", "")
    assert agent._is_notification_email("Your verification code is 123456", "")
    assert agent._is_notification_email("New login to your account", "")
    assert agent._is_notification_email("Password Reset requested", "")


def test_content_match(agent: AgentEmail):
    """Notification patterns in body are detected."""
    assert agent._is_notification_email(
        "Hello",
        "This is an automated message from the system.",
    )


def test_normal_email_not_filtered(agent: AgentEmail):
    """Regular emails are not flagged as notifications."""
    assert not agent._is_notification_email(
        "Re: Project update",
        "Hey, just wanted to follow up on the project.",
    )


def test_quoted_content_stripped(agent: AgentEmail):
    """Notification patterns in quoted lines (>) are ignored."""
    content = (
        "Thanks for the update!\n"
        "\n"
        "> Original message from github.com/notifications:\n"
        "> You are receiving this because you were mentioned."
    )
    assert not agent._is_notification_email("Re: Your PR", content)


def test_quoted_content_with_leading_whitespace(agent: AgentEmail):
    """Quoted lines with leading whitespace before > are also stripped."""
    content = (
        "Looks good to me.\n"
        "\n"
        " > This is an automated message from github.com/notifications\n"
        "  >> Nested quote with notification pattern"
    )
    assert not agent._is_notification_email("Re: Review", content)


def test_noreply_in_content(agent: AgentEmail):
    """no-reply pattern in unquoted content is detected."""
    assert agent._is_notification_email(
        "Info",
        "This email was sent from a no-reply address.",
    )


def test_noreply_in_quoted_content_ignored(agent: AgentEmail):
    """no-reply pattern in quoted content is ignored."""
    content = "Thanks!\n\n> Sent from no-reply@example.com"
    assert not agent._is_notification_email("Re: Hello", content)
