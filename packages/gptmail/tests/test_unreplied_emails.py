"""Tests for unreplied email discovery ordering."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from gptmail.lib import AgentEmail, UnrepliedEmail


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
    in_reply_to: str | None = None,
) -> None:
    lines = [
        f"Message-ID: {message_id}",
        f"From: Friend <{sender}>",
        f"To: {recipient}",
    ]
    if date_header is not None:
        lines.append(f"Date: {date_header}")
    if in_reply_to is not None:
        lines.append(f"In-Reply-To: {in_reply_to}")
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

    assert [entry.message_id for entry in unreplied] == [
        "<older@example.com>",
        "<newer@example.com>",
    ]
    assert [entry.folder for entry in unreplied] == ["inbox", "inbox"]


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

    assert [entry.message_id for entry in unreplied] == [
        "<malformed@example.com>",
        "<missing@example.com>",
        "<valid@example.com>",
    ]


def test_get_unreplied_emails_returns_typed_records(agent: AgentEmail) -> None:
    archive = agent.email_dir / "archive"
    expected_date = datetime(2026, 5, 16, 11, 0, tzinfo=timezone.utc)

    _write_email(
        archive / "typed-record.md",
        message_id="<typed@example.com>",
        subject="Typed record",
        sender="friend@example.com",
        recipient="test@example.com",
        date=expected_date,
    )

    unreplied = agent.get_unreplied_emails(folders=["archive"])

    assert unreplied == [
        UnrepliedEmail(
            message_id="<typed@example.com>",
            subject="Typed record",
            sender="friend@example.com",
            date=expected_date,
            folder="archive",
        )
    ]


def test_get_unreplied_emails_accepts_agent_email_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    email_dir = tmp_path / "email"
    for subdir in ["inbox", "sent", "archive", "drafts", "filters"]:
        (email_dir / subdir).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EMAIL_ALLOWLIST", "friend@example.com")
    monkeypatch.setenv("AGENT_EMAIL_ALIASES", "alias@example.com")
    agent = AgentEmail(str(tmp_path), "test@example.com")

    _write_email(
        email_dir / "inbox" / "alias-recipient.md",
        message_id="<alias@example.com>",
        subject="Alias recipient",
        sender="friend@example.com",
        recipient="Bob <alias@example.com>",
        date=datetime(2026, 5, 16, 11, 30, tzinfo=timezone.utc),
    )
    _write_email(
        email_dir / "inbox" / "other-recipient.md",
        message_id="<other@example.com>",
        subject="Other recipient",
        sender="friend@example.com",
        recipient="other@example.com",
        date=datetime(2026, 5, 16, 11, 31, tzinfo=timezone.utc),
    )

    unreplied = agent.get_unreplied_emails()

    assert [entry.message_id for entry in unreplied] == ["<alias@example.com>"]


def test_process_unreplied_emails_keeps_legacy_callback_contract(agent: AgentEmail) -> None:
    inbox = agent.email_dir / "inbox"
    expected_date = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)

    _write_email(
        inbox / "legacy-callback.md",
        message_id="<legacy@example.com>",
        subject="Legacy callback",
        sender="friend@example.com",
        recipient="test@example.com",
        date=expected_date,
    )

    seen: list[tuple[str, str, str]] = []

    def callback(message_id: str, subject: str, sender: str) -> None:
        seen.append((message_id, subject, sender))

    processed = agent.process_unreplied_emails(callback)

    assert processed == 1
    assert seen == [("<legacy@example.com>", "Legacy callback", "friend@example.com")]


def test_process_unreplied_emails_exposes_optional_metadata(agent: AgentEmail) -> None:
    archive = agent.email_dir / "archive"
    expected_date = datetime(2026, 5, 16, 13, 0, tzinfo=timezone.utc)

    _write_email(
        archive / "metadata-callback.md",
        message_id="<metadata@example.com>",
        subject="Metadata callback",
        sender="friend@example.com",
        recipient="test@example.com",
        date=expected_date,
    )

    seen: list[tuple[str, datetime, str, UnrepliedEmail]] = []

    def callback(
        message_id: str,
        subject: str,
        sender: str,
        *,
        date: datetime,
        folder: str,
        email_item: UnrepliedEmail,
    ) -> None:
        assert message_id == "<metadata@example.com>"
        assert subject == "Metadata callback"
        assert sender == "friend@example.com"
        seen.append((message_id, date, folder, email_item))

    processed = agent.process_unreplied_emails(callback, folders=["archive"])

    assert processed == 1
    assert seen == [
        (
            "<metadata@example.com>",
            expected_date,
            "archive",
            UnrepliedEmail(
                message_id="<metadata@example.com>",
                subject="Metadata callback",
                sender="friend@example.com",
                date=expected_date,
                folder="archive",
            ),
        )
    ]


def test_list_messages_keeps_malformed_dates_last(agent: AgentEmail) -> None:
    inbox = agent.email_dir / "inbox"

    _write_email(
        inbox / "valid-date.md",
        message_id="<valid@example.com>",
        subject="Valid date",
        sender="friend@example.com",
        recipient="test@example.com",
        date=datetime(2000, 1, 1, 10, 0, tzinfo=timezone.utc),
    )
    _write_email_with_raw_date(
        inbox / "malformed-date.md",
        message_id="<malformed@example.com>",
        subject="Malformed date",
        sender="friend@example.com",
        recipient="test@example.com",
        date_header="definitely not a real date",
    )

    messages = agent.list_messages("inbox")

    assert [message_id for message_id, _, _ in messages] == [
        "<valid@example.com>",
        "<malformed@example.com>",
    ]


def test_get_thread_messages_keeps_malformed_dates_last(agent: AgentEmail) -> None:
    inbox = agent.email_dir / "inbox"
    sent = agent.email_dir / "sent"

    root_id = "<root@example.com>"
    reply_id = "<reply@example.com>"

    _write_email(
        inbox / agent._format_filename(root_id),
        message_id=root_id,
        subject="Root message",
        sender="friend@example.com",
        recipient="test@example.com",
        date=datetime(2000, 1, 1, 10, 0, tzinfo=timezone.utc),
    )
    _write_email_with_raw_date(
        sent / agent._format_filename(reply_id),
        message_id=reply_id,
        subject="Reply message",
        sender="test@example.com",
        recipient="friend@example.com",
        date_header="definitely not a real date",
        in_reply_to=root_id,
    )

    thread_messages = agent.get_thread_messages(root_id)

    assert [message["id"] for message in thread_messages] == [root_id, reply_id]
