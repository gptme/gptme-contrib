"""Synthetic final-payload checks; no msmtp configuration or network access."""

import subprocess
import sys
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

import pytest

from gptmail.lib import AgentEmail


@pytest.fixture
def recording_send(tmp_path, monkeypatch):
    for folder in ("drafts", "sent", "inbox", "archive", "filters"):
        (tmp_path / "email" / folder).mkdir(parents=True)
    agent = AgentEmail(str(tmp_path), "bob@example.com")
    monkeypatch.setenv("EMAIL_SEND_ALLOWLIST", "recipient@example.com")
    monkeypatch.setattr(agent, "_validate_msmtp_config", lambda: True)
    monkeypatch.setattr(agent, "_get_msmtp_account_for_address", lambda sender: None)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setitem(
        sys.modules,
        "redact",
        SimpleNamespace(
            collect_secret_literals=lambda: ["synthetic forbidden phrase"],
            redact=lambda text, literals: SimpleNamespace(
                blocked="synthetic forbidden phrase" in text,
                block_reasons=["1× literal (critical)"],
            ),
        ),
    )
    draft = tmp_path / "email/drafts/fixture.md"
    return agent, draft, calls


@pytest.mark.parametrize("body", ["Hello", "<html><body>Hello</body></html>"])
def test_folded_secret_header_refused_before_delivery(recording_send, body, capsys):
    agent, draft, calls = recording_send
    draft.write_text("To: recipient@example.com\nSubject: synthetic\n forbidden phrase\n\n" + body)
    with pytest.raises(ValueError, match="Outbound"):
        agent.send("fixture")
    assert calls == []
    assert draft.exists()
    assert not (agent.email_dir / "sent/fixture.md").exists()
    captured = capsys.readouterr()
    assert "synthetic forbidden phrase" not in captured.out + captured.err


@pytest.mark.parametrize("body", ["Hello", "<html><body>Hello</body></html>"])
def test_clean_payload_delivered_once_and_archived(recording_send, body):
    agent, draft, calls = recording_send
    draft.write_text("To: recipient@example.com\nSubject: Clean\n\n" + body)
    agent.send("fixture")
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == ["msmtp", "recipient@example.com"]
    message = BytesParser(policy=policy.default).parsebytes(kwargs["input"])
    assert str(message["Subject"]) == "Clean"
    assert "Hello" in str(kwargs["input"])
    assert not draft.exists()
    assert (agent.email_dir / "sent/fixture.md").exists()


def test_forbidden_recipient_remains_zero_delivery(recording_send):
    agent, draft, calls = recording_send
    draft.write_text("To: forbidden@example.com\nSubject: Clean\n\nHello")
    with pytest.raises(ValueError, match="allowlist"):
        agent.send("fixture")
    assert calls == []
    assert draft.exists()


@pytest.mark.parametrize("failure_at", [1, 2])
def test_scanner_exception_is_private_and_preserves_draft(
    recording_send, monkeypatch, capsys, failure_at
):
    agent, draft, calls = recording_send
    draft.write_text("To: recipient@example.com\nSubject: Clean\n\nHello")
    count = 0

    def scan(text, literals):
        nonlocal count
        count += 1
        if count == failure_at:
            raise RuntimeError("synthetic forbidden phrase")
        return SimpleNamespace(blocked=False)

    monkeypatch.setattr(sys.modules["redact"], "redact", scan)
    with pytest.raises(ValueError, match="Outbound secret scan unavailable"):
        agent.send("fixture")
    assert not calls
    assert draft.exists()
    assert not (agent.email_dir / "sent/fixture.md").exists()
    captured = capsys.readouterr()
    assert "synthetic forbidden phrase" not in captured.out + captured.err


def test_rfc2047_decoded_header_is_scanned(recording_send):
    agent, draft, calls = recording_send
    draft.write_text(
        "To: recipient@example.com\nSubject: =?utf-8?q?synthetic_forbidden_phrase?=\n\nHello"
    )
    with pytest.raises(ValueError, match="Outbound"):
        agent.send("fixture")
    assert not calls
    assert draft.exists()


def test_rendered_html_alternative_is_scanned(recording_send, monkeypatch):
    import gptmail.lib as lib

    agent, draft, calls = recording_send
    draft.write_text("To: recipient@example.com\nSubject: Clean\n\nHello")
    # Rendering seam is synthetic: proves checking generated alternatives, not
    # an assertion that the Markdown library normally invents credentials.
    monkeypatch.setattr(
        lib.markdown, "markdown", lambda *a, **kw: "<p>synthetic forbidden phrase</p>"
    )
    with pytest.raises(ValueError, match="Outbound"):
        agent.send("fixture")
    assert not calls
    assert draft.exists()


def test_default_sender_header_is_scanned(recording_send):
    agent, draft, calls = recording_send
    agent.own_email = "synthetic forbidden phrase <bob@example.com>"
    draft.write_text("To: recipient@example.com\nSubject: Clean\n\nHello")
    with pytest.raises(ValueError, match="Outbound"):
        agent.send("fixture")
    assert not calls
    assert draft.exists()


def test_normal_markdown_escape_removal_is_scanned(recording_send, monkeypatch):
    agent, draft, calls = recording_send
    literal = "SYNTHETIC_TOKEN_ONLY"
    monkeypatch.setattr(sys.modules["redact"], "collect_secret_literals", lambda: [literal])
    monkeypatch.setattr(
        sys.modules["redact"],
        "redact",
        lambda text, literals: SimpleNamespace(
            blocked=literal in text, block_reasons=["1× literal (critical)"]
        ),
    )
    # Real Markdown turns escaped underscores into a contiguous HTML literal.
    draft.write_text("To: recipient@example.com\nSubject: Clean\n\nSYNTHETIC\\_TOKEN\\_ONLY")
    assert literal not in draft.read_text()
    with pytest.raises(ValueError, match="Outbound"):
        agent.send("fixture")
    assert not calls
    assert draft.exists()


@pytest.mark.parametrize("body", ["Hello", "<html><body>Hello</body></html>"])
@pytest.mark.parametrize(
    "comment",
    [
        "=?utf-8?q?synthetic_forbidden_phrase?=",
        "=?utf-8?b?c3ludGhldGljIGZvcmJpZGRlbiBwaHJhc2U=?=",
        "=?utf-8?q?synthetic_?=\n =?utf-8?q?forbidden_phrase?=",
    ],
)
def test_encoded_date_comment_is_scanned(recording_send, body, comment, capsys):
    agent, draft, calls = recording_send
    draft.write_text(
        "To: recipient@example.com\nSubject: Clean\n"
        f"Date: Tue, 01 Jan 2030 12:00:00 +0000 ({comment})\n\n" + body
    )
    original = draft.read_text()
    assert "synthetic forbidden phrase" not in original
    with pytest.raises(ValueError, match="Outbound"):
        agent.send("fixture")
    assert not calls
    assert draft.read_text() == original
    assert not (agent.email_dir / "sent/fixture.md").exists()
    captured = capsys.readouterr()
    assert "synthetic forbidden phrase" not in captured.out + captured.err


@pytest.mark.parametrize("body", ["Hello", "<html><body>Hello</body></html>"])
def test_folded_date_comment_is_not_lost_by_semantic_parser(recording_send, body):
    agent, draft, calls = recording_send
    draft.write_text(
        "To: recipient@example.com\nSubject: Clean\n"
        "Date: Tue, 01 Jan 2030 12:00:00 +0000 (synthetic\n forbidden phrase)\n\n" + body
    )
    with pytest.raises(ValueError, match="Outbound"):
        agent.send("fixture")
    assert not calls
    assert draft.exists()


@pytest.mark.parametrize("body", ["Hello", "<html><body>Hello</body></html>"])
@pytest.mark.parametrize(
    "encoded", ["=?x-example?q?hello?=", "=?utf-8?b?/w==?=", "=?idna?q?hello?="]
)
def test_unrecognized_or_invalid_header_charset_remains_deliverable(recording_send, body, encoded):
    agent, draft, calls = recording_send
    subject = f"Discuss {encoded}"
    draft.write_text(f"To: recipient@example.com\nSubject: {subject}\n\n{body}")
    agent.send("fixture")
    assert len(calls) == 1
    message = BytesParser(policy=policy.default).parsebytes(calls[0][1]["input"])
    assert dict(message.raw_items())["Subject"] == subject
    assert not draft.exists()
    assert (agent.email_dir / "sent/fixture.md").exists()


@pytest.mark.parametrize("body", ["Hello", "<html><body>Hello</body></html>"])
@pytest.mark.parametrize(
    "encoded",
    [
        "=?x-example?q?synthetic_forbidden_phrase?=",
        "=?x-example?q?hello?= =?utf-8?q?synthetic_forbidden_phrase?=",
    ],
)
def test_unknown_charset_does_not_skip_decoded_secret_scan(recording_send, body, encoded, capsys):
    agent, draft, calls = recording_send
    draft.write_text(f"To: recipient@example.com\nSubject: {encoded}\n\n{body}")
    original = draft.read_text()
    with pytest.raises(ValueError, match="Outbound"):
        agent.send("fixture")
    assert not calls
    assert draft.read_text() == original
    assert not (agent.email_dir / "sent/fixture.md").exists()
    captured = capsys.readouterr()
    assert "synthetic forbidden phrase" not in captured.out + captured.err


@pytest.mark.parametrize(
    "body",
    [
        "SYNTHETIC\\_TOKEN&ONLY",
        "<html><body>SYNTHETIC_TOKEN&amp;ONLY</body></html>",
        "<html><body>SYNTHETIC_TOKEN&#38;ONLY</body></html>",
        "<html><body>SYNTHETIC_TOKEN&#x26;ONLY</body></html>",
    ],
)
def test_html_character_references_are_scanned(recording_send, monkeypatch, body, capsys):
    agent, draft, calls = recording_send
    literal = "SYNTHETIC_TOKEN&ONLY"
    monkeypatch.setattr(sys.modules["redact"], "collect_secret_literals", lambda: [literal])
    monkeypatch.setattr(
        sys.modules["redact"],
        "redact",
        lambda text, literals: SimpleNamespace(
            blocked=literal in text, block_reasons=["1× literal (critical)"]
        ),
    )
    draft.write_text(f"To: recipient@example.com\nSubject: Clean\n\n{body}")
    original = draft.read_text()
    assert literal not in original
    with pytest.raises(ValueError, match="Outbound"):
        agent.send("fixture")
    assert not calls
    assert draft.read_text() == original
    assert not (agent.email_dir / "sent/fixture.md").exists()
    captured = capsys.readouterr()
    assert literal not in captured.out + captured.err
