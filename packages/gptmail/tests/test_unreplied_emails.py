"""Tests for unreplied email discovery ordering."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from gptmail.lib import AgentEmail


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgentEmail:
    """Create a minimal AgentEmail with an explicit allowlist."""
    email_dir = tmp_path / "email"
    for subdir in ["inbox", "sent", "archive", "drafts", "filters"]:
        (email_dir / subdir).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EMAIL_ALLOWLIST", "friend@example.com")
    return AgentEmail(str(tmp_path), "test@example.com")


def _write_email(
    path: Path,
    *,
    message_id: str,
    subject: str,
    sender: str,
    recipient: str,
    date: datetime,
) -> None:
    path.write_text(
        "\n".join(
            [
                f"Message-ID: {message_id}",
                f"From: Friend <{sender}>",
                f"To: {recipient}",
                f"Date: {date.strftime('%a, %d %b %Y %H:%M:%S %z')}",
                f"Subject: {subject}",
                "",
                "Body",
            ]
        )
    )


def _write_email_with_raw_date(
    path: Path,
    *,
    message_id: str,
    subject: str,
    sender: str,
    recipient: str,
    date_header: str | None,
) -> None:
    lines = [
        f"Message-ID: {message_id}",
        f"From: Friend <{sender}>",
        f"To: {recipient}",
    ]
    if date_header is not None:
        lines.append(f"Date: {date_header}")
    lines.extend(
        [
            f"Subject: {subject}",
            "",
            "Body",
        ]
    )
    path.write_text("\n".join(lines))


def test_get_unreplied_emails_sorts_oldest_first(
    agent: AgentEmail,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox = agent.email_dir / "inbox"
    older = inbox / "z-older.md"
    newer = inbox / "a-newer.md"

    _write_email(
        older,
        message_id="<older@example.com>",
        subject="Older",
        sender="friend@example.com",
        recipient="test@example.com",
        date=datetime(2026, 5, 16, 9, 0, tzinfo=timezone.utc),
    )
    _write_email(
        newer,
        message_id="<newer@example.com>",
        subject="Newer",
        sender="friend@example.com",
        recipient="test@example.com",
        date=datetime(2026, 5, 16, 10, 0, tzinfo=timezone.utc),
    )

    original_glob = Path.glob

    def reversed_inbox_glob(self: Path, pattern: str):
        if self == inbox and pattern == "*.md":
            return iter([newer, older])
        return original_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", reversed_inbox_glob)

    unreplied = agent.get_unreplied_emails()

    assert [message_id for message_id, _, _ in unreplied] == [
        "<older@example.com>",
        "<newer@example.com>",
    ]


def test_get_unreplied_emails_treats_missing_and_malformed_dates_as_oldest(
    agent: AgentEmail,
) -> None:
    inbox = agent.email_dir / "inbox"

    _write_email_with_raw_date(
        inbox / "b-missing-date.md",
        message_id="<missing@example.com>",
        subject="Missing date",
        sender="friend@example.com",
        recipient="test@example.com",
        date_header=None,
    )
    _write_email_with_raw_date(
        inbox / "a-malformed-date.md",
        message_id="<malformed@example.com>",
        subject="Malformed date",
        sender="friend@example.com",
        recipient="test@example.com",
        date_header="definitely not a real date",
    )
    _write_email(
        inbox / "c-valid-date.md",
        message_id="<valid@example.com>",
        subject="Valid date",
        sender="friend@example.com",
        recipient="test@example.com",
        date=datetime(2026, 5, 16, 10, 0, tzinfo=timezone.utc),
    )

    unreplied = agent.get_unreplied_emails()

    assert [message_id for message_id, _, _ in unreplied] == [
        "<malformed@example.com>",
        "<missing@example.com>",
        "<valid@example.com>",
    ]
