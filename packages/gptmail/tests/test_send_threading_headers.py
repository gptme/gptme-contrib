"""Bare threading headers in a draft must not be sent as empty headers (#1847)."""

import subprocess
from pathlib import Path

import pytest

from gptmail.lib import AgentEmail


@pytest.fixture
def agent(tmp_path: Path) -> AgentEmail:
    email_dir = tmp_path / "email"
    for subdir in ["inbox", "sent", "archive", "drafts", "filters"]:
        (email_dir / subdir).mkdir(parents=True, exist_ok=True)
    return AgentEmail(str(tmp_path), "bob@gptme.org")


def _sent_bytes(
    agent: AgentEmail, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, draft: str
) -> bytes:
    monkeypatch.setenv("EMAIL_SEND_ALLOWLIST", "*")
    (tmp_path / "email" / "drafts" / "thread-test.md").write_text(draft)
    sent: list[bytes] = []

    def deliver(*args, **kwargs):
        sent.append(kwargs.get("input") or b"")
        return subprocess.CompletedProcess(args[0], 0)

    monkeypatch.setattr(subprocess, "run", deliver)
    agent.send("thread-test")
    assert sent, "no delivery attempted"
    return sent[-1]


@pytest.mark.parametrize("body", ["Hello\n", "<html><body>Hello</body></html>\n"])
def test_bare_threading_headers_are_omitted(
    agent: AgentEmail, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    raw = _sent_bytes(
        agent,
        tmp_path,
        monkeypatch,
        "To: erik@example.com\nSubject: Hi\nIn-Reply-To:\nReferences:\n\n" + body,
    )
    header_block = raw.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0]
    assert b"In-Reply-To" not in header_block
    assert b"References" not in header_block


def test_real_threading_headers_are_kept(
    agent: AgentEmail, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _sent_bytes(
        agent,
        tmp_path,
        monkeypatch,
        "To: erik@example.com\nSubject: Re: Hi\nIn-Reply-To: <a@b>\n"
        "References: <a@b>\n\nHello\n",
    )
    assert b"In-Reply-To: <a@b>" in raw
    assert b"References: <a@b>" in raw
