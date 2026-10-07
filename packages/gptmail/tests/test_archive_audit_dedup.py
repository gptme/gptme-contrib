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
