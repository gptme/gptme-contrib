"""Inbox dedup must not drop a different sender's identical reply (gptme/gptme-contrib#2303 item 1)."""

from email.message import EmailMessage
from pathlib import Path

import pytest
from gptmail.lib import AgentEmail


@pytest.fixture(autouse=True)
def email_structure(tmp_path: Path) -> None:
    for folder in ["inbox", "sent", "archive", "drafts", "filters"]:
        (tmp_path / "email" / folder).mkdir(parents=True)


def _stored(tmp_path: Path, sender: str) -> None:
    (tmp_path / "email" / "inbox" / "first.md").write_text(
        f"From: {sender}\nTo: bob@gptme.org\nSubject: Re: Lunch?\n"
        "Date: Wed, 07 Oct 2026 11:00:00 +0000\n"
        "Message-ID: <first@example.com>\nIn-Reply-To: <lunch@gptme.org>\n\n"
        "Sounds good!\n"
    )


def _incoming(sender: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "bob@gptme.org"
    msg["Subject"] = "Re: Lunch?"
    msg["Date"] = "Wed, 07 Oct 2026 11:05:00 +0000"
    msg["Message-ID"] = "<second@example.com>"
    msg["In-Reply-To"] = "<lunch@gptme.org>"
    msg.set_content("Sounds good!")
    return msg


def test_same_reply_text_from_another_sender_is_not_a_duplicate(tmp_path: Path) -> None:
    _stored(tmp_path, "alice@example.com")
    agent = AgentEmail(str(tmp_path), "bob@gptme.org")
    assert not agent._is_duplicate_message(_incoming("carol@example.com"), "inbox")


def test_same_reply_from_the_same_sender_is_still_a_duplicate(tmp_path: Path) -> None:
    _stored(tmp_path, "alice@example.com")
    agent = AgentEmail(str(tmp_path), "bob@gptme.org")
    assert agent._is_duplicate_message(_incoming("Alice <alice@example.com>"), "inbox")


def test_resync_dedups_when_several_senders_replied(tmp_path: Path) -> None:
    """Every reply in a thread shares the primary index key.

    Dedup has to see all of them: a re-synced copy of one sender's reply must
    still be caught when another sender's reply owns the same key.
    """
    _stored(tmp_path, "alice@example.com")
    (tmp_path / "email" / "inbox" / "second.md").write_text(
        "From: carol@example.com\nTo: bob@gptme.org\nSubject: Re: Lunch?\n"
        "Date: Wed, 07 Oct 2026 11:00:30 +0000\n"
        "Message-ID: <second@example.com>\nIn-Reply-To: <lunch@gptme.org>\n\n"
        "Sounds good!\n"
    )
    agent = AgentEmail(str(tmp_path), "bob@gptme.org")

    # Gmail re-syncs both replies with a reformatted display name and a new
    # Message-ID. Whichever file won the shared index key, both must dedup.
    assert agent._is_duplicate_message(_incoming("Alice <alice@example.com>"), "inbox")
    assert agent._is_duplicate_message(_incoming("Carol <carol@example.com>"), "inbox")
