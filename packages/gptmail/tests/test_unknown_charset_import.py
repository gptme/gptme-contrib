"""A text part with an unknown charset must still import (gptme/gptme-contrib#2303 item 2)."""

import email
from pathlib import Path

import pytest
from gptmail.lib import AgentEmail

HEADERS = (
    "From: Friend <friend@example.com>\n"
    "To: test@example.com\n"
    "Subject: Odd charset\n"
    "Date: Wed, 07 Oct 2026 11:00:00 +0000\n"
    "Message-ID: <odd-charset@example.com>\n"
    "MIME-Version: 1.0\n"
)
PART = (
    "Content-Type: text/plain; charset=unknown-8bit\n"
    "Content-Transfer-Encoding: 8bit\n"
    "\n"
    "Hello there\n"
)
SINGLE = HEADERS + PART
MULTIPART = (
    HEADERS + 'Content-Type: multipart/alternative; boundary="b"\n\n' + "--b\n" + PART + "--b--\n"
)


@pytest.mark.parametrize("multipart", [False, True])
def test_unknown_charset_part_is_imported(tmp_path: Path, multipart: bool) -> None:
    for sub in ["inbox", "sent", "archive", "drafts", "filters"]:
        (tmp_path / "email" / sub).mkdir(parents=True)
    agent = AgentEmail(str(tmp_path), "test@example.com")
    maildir = tmp_path / "maildir"
    (maildir / "new").mkdir(parents=True)
    mid = "<odd-charset@example.com>"
    raw = MULTIPART if multipart else SINGLE
    # Guard against a vacuous fixture: the message must have the intended shape.
    parsed = email.message_from_string(raw)
    assert parsed.is_multipart() is multipart
    leaf = parsed.get_payload(0) if multipart else parsed
    assert leaf.get_content_charset() == "unknown-8bit"
    (maildir / "new" / "odd").write_bytes(raw.encode())
    agent.external_maildir_folders["inbox"] = maildir

    agent.sync_from_maildir("inbox")

    imported = agent.email_dir / "inbox" / agent._format_filename(mid)
    assert imported.exists()
    assert "Hello there" in imported.read_text()


def test_non_text_part_body_is_decoded_not_repr() -> None:
    """A non-text part's ``get_content()`` is bytes; it must decode, not become ``b'...'``."""
    from email import policy

    from gptmail.lib import _part_text

    raw = (
        HEADERS
        + "Content-Type: application/octet-stream\n"
        + "Content-Transfer-Encoding: 8bit\n\n"
        + "Hello there\nsecond line\n"
    )
    msg = email.message_from_string(raw, policy=policy.default)
    assert isinstance(msg.get_content(), bytes)  # the shape under test

    text = _part_text(msg)

    assert text.startswith("Hello there\nsecond line")
    assert "b'" not in text
