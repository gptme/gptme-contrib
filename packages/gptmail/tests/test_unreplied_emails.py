"""Tests for unreplied email discovery ordering."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

import gptmail.cli as gptmail_cli
from gptmail.lib import AgentEmail, UnrepliedEmail, inbound_recipient_role


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


def _write_maildir_email(
    path: Path,
    *,
    message_id: str,
    subject: str,
    to: str,
    cc: str | None = None,
    bcc: str | None = None,
    delivered_to: str | None = None,
    from_header: str = "Friend <friend@example.com>",
    extra_headers: list[str] | None = None,
    body: str = "Body",
) -> None:
    lines = [
        f"From: {from_header}",
        f"To: {to}",
    ]
    if cc is not None:
        lines.append(f"Cc: {cc}")
    if bcc is not None:
        lines.append(f"Bcc: {bcc}")
    if delivered_to is not None:
        lines.append(f"Delivered-To: {delivered_to}")
    if extra_headers:
        lines.extend(extra_headers)
    lines.extend(
        [
            "Date: Tue, 08 Sep 2026 16:00:00 +0000",
            f"Subject: {subject}",
            f"Message-ID: {message_id}",
            "Content-Type: text/plain; charset=utf-8",
            "",
            body,
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


def test_inbound_recipient_role_ignores_body_to_line() -> None:
    headers = {
        "To": "other@example.com",
        "From": "Friend <friend@example.com>",
    }
    assert inbound_recipient_role(headers, ("test@example.com",)) is None


def test_maildir_sync_preserves_envelope_for_stripped_bcc(
    agent: AgentEmail, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gmail strips Bcc; Delivered-To on the agent's alias is the provenance."""
    monkeypatch.setenv("AGENT_EMAIL_ALIASES", "alias@example.com")
    email_dir = tmp_path / "email"
    for subdir in ["inbox", "sent", "archive", "drafts", "filters"]:
        (email_dir / subdir).mkdir(parents=True, exist_ok=True)
    aliased = AgentEmail(str(tmp_path), "test@example.com")

    maildir = tmp_path / "maildir"
    (maildir / "new").mkdir(parents=True)
    message_id = "<stripped-bcc@example.com>"
    _write_maildir_email(
        maildir / "new" / "stripped-bcc",
        message_id=message_id,
        subject="Stripped BCC",
        to="other@example.com",
        delivered_to="alias@example.com",
        body="Forwarded copy\nTo: Bob <test@example.com>\nshould not count",
    )
    aliased.external_maildir_folders["inbox"] = maildir

    aliased.sync_from_maildir("inbox")

    imported = (aliased.email_dir / "inbox" / aliased._format_filename(message_id)).read_text()
    header_block = imported.split("\n\n", 1)[0]
    assert "Delivered-To: alias@example.com" in header_block
    assert "To: other@example.com" in header_block
    assert [line for line in header_block.splitlines() if line.startswith("To:")] == [
        "To: other@example.com"
    ]
    assert [entry.message_id for entry in aliased.get_unreplied_emails()] == [message_id]


def test_maildir_sync_preserves_explicit_bcc_for_unreplied_detection(
    agent: AgentEmail, tmp_path: Path
) -> None:
    maildir = tmp_path / "maildir"
    (maildir / "new").mkdir(parents=True)
    message_id = "<direct-bcc@example.com>"
    _write_maildir_email(
        maildir / "new" / "direct-bcc",
        message_id=message_id,
        subject="Direct BCC",
        to="other@example.com",
        bcc="Bob <test@example.com>",
    )
    agent.external_maildir_folders["inbox"] = maildir

    agent.sync_from_maildir("inbox")

    imported = (agent.email_dir / "inbox" / agent._format_filename(message_id)).read_text()
    assert "Bcc: Bob <test@example.com>" in imported
    assert [entry.message_id for entry in agent.get_unreplied_emails()] == [message_id]


def test_cc_is_listed_but_not_auto_replied(agent: AgentEmail, tmp_path: Path) -> None:
    maildir = tmp_path / "maildir"
    (maildir / "new").mkdir(parents=True)
    message_id = "<cc-only@example.com>"
    _write_maildir_email(
        maildir / "new" / "cc-only",
        message_id=message_id,
        subject="CC only",
        to="other@example.com",
        cc="Bob <test@example.com>",
        delivered_to="test@example.com",
    )
    agent.external_maildir_folders["inbox"] = maildir

    agent.sync_from_maildir("inbox")

    imported = (agent.email_dir / "inbox" / agent._format_filename(message_id)).read_text()
    assert "Cc: Bob <test@example.com>" in imported
    assert agent.get_unreplied_emails() == []
    listed_ids = [message_id_ for message_id_, _, _ in agent.list_messages("inbox")]
    assert listed_ids == [message_id]


def test_body_to_line_is_not_delivery_evidence(agent: AgentEmail) -> None:
    inbox = agent.email_dir / "inbox"
    _write_email_with_raw_date(
        inbox / "body-to.md",
        message_id="<body-to@example.com>",
        subject="Body To trap",
        sender="friend@example.com",
        recipient="other@example.com",
        date_header="Tue, 08 Sep 2026 16:00:00 +0000",
    )
    path = inbox / "body-to.md"
    path.write_text(path.read_text() + "To: Bob <test@example.com>\n")

    assert agent.get_unreplied_emails() == []


def test_stripped_bcc_keeps_allowlist_and_self_exclusions(
    agent: AgentEmail, tmp_path: Path
) -> None:
    maildir = tmp_path / "maildir"
    (maildir / "new").mkdir(parents=True)
    _write_maildir_email(
        maildir / "new" / "untrusted",
        message_id="<untrusted-bcc@example.com>",
        subject="Untrusted BCC",
        to="other@example.com",
        delivered_to="test@example.com",
        from_header="Stranger <stranger@example.com>",
    )
    _write_maildir_email(
        maildir / "new" / "self-sent",
        message_id="<self-bcc@example.com>",
        subject="Self BCC",
        to="other@example.com",
        delivered_to="test@example.com",
        from_header="Bob <test@example.com>",
    )
    agent.external_maildir_folders["inbox"] = maildir
    agent.sync_from_maildir("inbox")

    assert agent.get_unreplied_emails() == []


def test_stripped_bcc_respects_completed_and_already_replied(agent: AgentEmail) -> None:
    inbox = agent.email_dir / "inbox"
    completed_id = "<completed-bcc@example.com>"
    replied_id = "<replied-bcc@example.com>"
    inbox.joinpath(agent._format_filename(completed_id)).write_text(
        "\n".join(
            [
                "From: Friend <friend@example.com>",
                "To: other@example.com",
                "Delivered-To: test@example.com",
                "Date: Tue, 08 Sep 2026 16:00:00 +0000",
                "Subject: Completed BCC",
                f"Message-ID: {completed_id}",
                "",
                "Body",
            ]
        )
    )
    inbox.joinpath(agent._format_filename(replied_id)).write_text(
        "\n".join(
            [
                "From: Friend <friend@example.com>",
                "To: other@example.com",
                "Delivered-To: test@example.com",
                "Date: Tue, 08 Sep 2026 16:01:00 +0000",
                "Subject: Replied BCC",
                f"Message-ID: {replied_id}",
                "",
                "Body",
            ]
        )
    )
    agent._mark_no_reply_needed(completed_id, "informational")
    (agent.email_dir / "sent" / "already-replied.md").write_text(
        "\n".join(
            [
                "From: Bob <test@example.com>",
                "To: Friend <friend@example.com>",
                f"In-Reply-To: {replied_id}",
                "Subject: Re: Replied BCC",
                "Message-ID: <sent-reply@example.com>",
                "",
                "Already answered",
            ]
        )
    )

    assert agent.get_unreplied_emails() == []


def test_private_reply_targets_sender_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    email_dir = tmp_path / "email"
    for subdir in ["inbox", "sent", "archive", "drafts", "filters"]:
        (email_dir / subdir).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EMAIL_ALLOWLIST", "friend@example.com")
    monkeypatch.setenv("AGENT_EMAIL", "test@example.com")
    monkeypatch.setattr(gptmail_cli, "get_workspace_dir", lambda: tmp_path)

    message_id = "<private-bcc@example.com>"
    agent = AgentEmail(str(tmp_path), "test@example.com")
    (email_dir / "inbox" / agent._format_filename(message_id)).write_text(
        "\n".join(
            [
                "From: Friend <friend@example.com>",
                "To: Other <other@example.com>",
                "Cc: Spectator <spectator@example.com>",
                "Bcc: Bob <test@example.com>",
                "Delivered-To: test@example.com",
                "Date: Tue, 08 Sep 2026 16:00:00 +0000",
                "Subject: Private analysis",
                f"Message-ID: {message_id}",
                "",
                "Please look at this privately.",
            ]
        )
    )

    result = CliRunner().invoke(
        gptmail_cli.cli, ["reply", message_id, "Private note for you only."]
    )
    assert result.exit_code == 0, result.output
    drafts = list((email_dir / "drafts").glob("*.md"))
    assert len(drafts) == 1
    draft = drafts[0].read_text()
    draft_headers, _ = draft.split("\n\n", 1)
    assert "To: Friend <friend@example.com>" in draft_headers
    assert "other@example.com" not in draft_headers
    assert "spectator@example.com" not in draft_headers
    assert not any(line.lower().startswith("bcc:") for line in draft_headers.splitlines())
    assert not any(line.lower().startswith("cc:") for line in draft_headers.splitlines())
    assert "Delivered-To:" not in draft_headers
    body = draft.split("\n\n", 1)[1].lower()
    assert "blind copy" not in body
    assert "bcc'd" not in body and "bccd" not in body
