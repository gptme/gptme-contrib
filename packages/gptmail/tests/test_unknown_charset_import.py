"""A text part with an unknown charset must still import (gptme/gptme-contrib#2303 item 2)."""

from pathlib import Path

import pytest
from gptmail.lib import AgentEmail

RAW = (
    "From: Friend <friend@example.com>\n"
    "To: test@example.com\n"
    "Subject: Odd charset\n"
    "Date: Wed, 07 Oct 2026 11:00:00 +0000\n"
    "Message-ID: {mid}\n"
    "MIME-Version: 1.0\n"
    "{ctype}\n"
    "Content-Transfer-Encoding: 8bit\n"
    "\n"
    "Hello there\n"
)
MULTIPART = (
    'Content-Type: multipart/alternative; boundary="b"\n\n'
    "--b\nContent-Type: text/plain; charset=unknown-8bit\n\nHello there\n--b--"
)


@pytest.mark.parametrize("multipart", [False, True])
def test_unknown_charset_part_is_imported(tmp_path: Path, multipart: bool) -> None:
    for sub in ["inbox", "sent", "archive", "drafts", "filters"]:
        (tmp_path / "email" / sub).mkdir(parents=True)
    agent = AgentEmail(str(tmp_path), "test@example.com")
    maildir = tmp_path / "maildir"
    (maildir / "new").mkdir(parents=True)
    mid = "<odd-charset@example.com>"
    if multipart:
        raw = RAW.format(mid=mid, ctype="").rsplit("\n\nHello there\n", 1)[0]
        raw = raw.replace("Content-Transfer-Encoding: 8bit\n", "") + MULTIPART + "\n"
    else:
        raw = RAW.format(mid=mid, ctype="Content-Type: text/plain; charset=unknown-8bit")
    (maildir / "new" / "odd").write_bytes(raw.encode())
    agent.external_maildir_folders["inbox"] = maildir

    agent.sync_from_maildir("inbox")

    imported = agent.email_dir / "inbox" / agent._format_filename(mid)
    assert imported.exists()
    assert "Hello there" in imported.read_text()
