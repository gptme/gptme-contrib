"""Local send-audit metadata must not change archive content identity."""

from email.message import EmailMessage
from pathlib import Path

import pytest

from gptmail.lib import AgentEmail

AUDIT = "<!-- send-audit: allowlist=default sent_at=2026-10-07T11:00:00Z -->"


@pytest.fixture(autouse=True)
def email_structure(tmp_path: Path) -> None:
    for folder in ["inbox", "sent", "archive", "drafts", "filters"]:
        (tmp_path / "email" / folder).mkdir(parents=True)


@pytest.mark.parametrize("body", ["Hello", "Hello\n\n", "x" * 180])
def test_archive_matches_copy_without_local_audit(tmp_path: Path, body: str) -> None:
    agent = AgentEmail(str(tmp_path), "bob@gptme.org")
    archive = tmp_path / "email" / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    headers = (
        "From: bob@gptme.org\nTo: erik@example.com\nSubject: Reply\n"
        "Date: Wed, 07 Oct 2026 11:00:00 +0000\nIn-Reply-To: <original@example.com>\n"
    )
    content = headers + "Message-ID: <local@example.com>\n\n" + body + "\n" + AUDIT + "\n"
    path = archive / "local.md"
    path.write_text(content)
    agent._record_local_send(path)
    incoming = EmailMessage()
    for line in headers.strip().splitlines():
        key, value = line.split(": ", 1)
        incoming[key] = value
    incoming["Message-ID"] = "<reassigned@example.com>"
    incoming.set_content(body)

    assert agent._is_duplicate_message(incoming, "archive")
    assert path.read_text() == content  # Comparison never modifies the stored audit.
    incoming.set_content("A genuinely different reply")
    assert not agent._is_duplicate_message(incoming, "archive")


@pytest.mark.parametrize(
    "suffix",
    [
        "<!-- send-audit: arbitrary user content -->",
        AUDIT + "\nMore message text",
        "<!-- send-audit: allowlist=default sent_at=invalid -->",
    ],
)
def test_non_trailing_or_unrecognized_comments_remain_part_of_body(
    tmp_path: Path, suffix: str
) -> None:
    agent = AgentEmail(str(tmp_path), "bob@gptme.org")
    archive = tmp_path / "email" / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    headers = "From: bob@gptme.org\nTo: erik@example.com\nSubject: Reply\nIn-Reply-To: <original>\n"
    (archive / "local.md").write_text(headers + "\nHello\n" + suffix + "\n")
    incoming = EmailMessage()
    incoming["Subject"] = "Reply"
    incoming["To"] = "erik@example.com"
    incoming["In-Reply-To"] = "<original>"
    incoming.set_content("Hello")
    assert not agent._is_duplicate_message(incoming, "archive")


@pytest.mark.parametrize("folder", ["inbox", "archive"])
@pytest.mark.parametrize("audit_on_incoming", [True, False])
def test_audit_shaped_incoming_content_is_not_local_metadata(
    tmp_path: Path, folder: str, audit_on_incoming: bool
) -> None:
    agent = AgentEmail(str(tmp_path), "bob@gptme.org")
    headers = (
        "From: other@example.com\nTo: bob@gptme.org\nSubject: Reply\nIn-Reply-To: <original>\n"
    )
    audited = "Hello\n" + AUDIT + "\n"
    existing_body = "Hello\n" if audit_on_incoming else audited
    (tmp_path / "email" / folder / "local.md").write_text(headers + "\n" + existing_body)
    incoming = EmailMessage()
    for line in headers.strip().splitlines():
        key, value = line.split(": ", 1)
        incoming[key] = value
    incoming.set_content(audited if audit_on_incoming else "Hello")

    assert not agent._is_duplicate_message(incoming, folder)


def test_received_mail_from_own_address_is_never_audit_stripped(
    tmp_path: Path,
) -> None:
    """Regression for the send-provenance P1: receive() -> archive() of a
    message From our own address must not treat it as locally audited, or a
    distinct plain reply with the same Subject/To/In-Reply-To is skipped."""
    agent = AgentEmail(str(tmp_path), "bob@gptme.org")
    headers = "From: bob@gptme.org\nTo: erik@example.com\nSubject: Reply\nIn-Reply-To: <original>\n"
    stored = "Hello\n" + AUDIT + "\n"
    path = tmp_path / "email" / "archive" / "loop.md"
    path.write_text(headers + "\n" + stored)
    incoming = EmailMessage()
    for line in headers.strip().splitlines():
        key, value = line.split(": ", 1)
        incoming[key] = value
    incoming["Message-ID"] = "<reply@example.com>"
    incoming.set_content("Hello")

    assert not agent._is_duplicate_message(incoming, "archive")


def test_recorded_local_send_grants_audit_stripping_after_archive(
    tmp_path: Path,
) -> None:
    """The authoritative record is what enables stripping, even in archive."""
    agent = AgentEmail(str(tmp_path), "bob@gptme.org")
    headers = (
        "From: bob@gptme.org\nTo: erik@example.com\nSubject: Reply\n"
        "In-Reply-To: <original@example.com>\n"
    )
    content = headers + "Message-ID: <local@example.com>\n\nHello\n" + AUDIT + "\n"
    path = tmp_path / "email" / "archive" / "local.md"
    path.write_text(content)
    incoming = EmailMessage()
    for line in headers.strip().splitlines():
        key, value = line.split(": ", 1)
        incoming[key] = value
    incoming["Message-ID"] = "<reassigned@example.com>"
    incoming.set_content("Hello")

    # Without the send record (e.g. received then archived): no match.
    assert not agent._is_duplicate_message(incoming, "archive")

    agent._record_local_send(path)
    agent._message_index_cache.clear()  # rebuild with the new provenance
    assert agent._is_duplicate_message(incoming, "archive")
    assert path.read_text() == content


def test_filename_reuse_by_different_body_never_grants_stripping(tmp_path: Path) -> None:
    """The record binds the digest, so a different message with the same
    filename (e.g. a Message-ID collision) is never audit-stripped."""
    agent = AgentEmail(str(tmp_path), "bob@gptme.org")
    sent_content = (
        "From: bob@gptme.org\nTo: erik@example.com\nSubject: Reply\n"
        "In-Reply-To: <original@example.com>\n"
        "Message-ID: <local@example.com>\n\nHello\n" + AUDIT + "\n"
    )
    sent_path = tmp_path / "email" / "sent" / "local.md"
    sent_path.write_text(sent_content)
    agent._record_local_send(sent_path)

    # A different message (spoofed echo / collision) reusing the same filename.
    other_content = (
        "From: bob@gptme.org\nTo: erik@example.com\nSubject: Reply\n"
        "In-Reply-To: <original@example.com>\n"
        "Message-ID: <local@example.com>\n\nHello\n" + AUDIT + "\nMore appended\n"
    )
    other_path = tmp_path / "email" / "archive" / "local.md"
    other_path.write_text(other_content)
    agent._message_index_cache = {}

    headers = (
        "From: bob@gptme.org\nTo: erik@example.com\nSubject: Reply\n"
        "In-Reply-To: <original@example.com>\n"
    )
    incoming = EmailMessage()
    for line in headers.strip().splitlines():
        key, value = line.split(": ", 1)
        incoming[key] = value
    incoming.set_content("A genuinely different reply")

    # Not a duplicate of the collision file: its body was not granted stripping.
    assert not agent._is_duplicate_message(incoming, "archive")


def test_incoming_copy_of_local_send_with_trailer_still_deduplicates(
    tmp_path: Path,
) -> None:
    """A re-read copy of a locally-sent message (same addresses, reassigned
    Message-ID) carries the trailer; matching against the raw stored snippet
    still finds it."""
    agent = AgentEmail(str(tmp_path), "bob@gptme.org")
    sent_content = (
        "From: bob@gptme.org\nTo: erik@example.com\nSubject: Reply\n"
        "Date: Wed, 07 Oct 2026 11:00:00 +0000\n"
        "In-Reply-To: <original@example.com>\n"
        "Message-ID: <local@example.com>\n\nHello\n" + AUDIT + "\n"
    )
    sent_path = tmp_path / "email" / "sent" / "local.md"
    sent_path.write_text(sent_content)
    agent._record_local_send(sent_path)

    incoming = EmailMessage()
    incoming["From"] = "bob@gptme.org"
    incoming["To"] = "erik@example.com"
    incoming["Subject"] = "Reply"
    incoming["Date"] = "Wed, 07 Oct 2026 11:00:30 +0000"
    incoming["In-Reply-To"] = "<original@example.com>"
    incoming.set_content("Hello\n" + AUDIT + "\n")

    assert agent._is_duplicate_message(incoming, "sent")
