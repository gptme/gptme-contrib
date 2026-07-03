"""Tests for MIME email construction in gptmail."""

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr

from gptmail.lib import _format_address_header, _is_html, _utf8_qp


def test_is_html_detects_html():
    """HTML content starting with <html> is detected."""
    assert _is_html("<html><body>hello</body></html>")
    assert _is_html("<HTML><BODY>hello</BODY></HTML>")
    assert _is_html("<!DOCTYPE html><html><body>hi</body></html>")
    assert _is_html("  <html>\n<body>hi</body></html>")  # leading whitespace


def test_is_html_rejects_non_html():
    """Plain text and markdown are not detected as HTML."""
    assert not _is_html("Hello world")
    assert not _is_html("# Heading\n\nSome text")
    assert not _is_html("<p>paragraph without html wrapper</p>")
    assert not _is_html("**bold** and *italic*")


def test_quoted_printable_encoding_ascii():
    """ASCII MIME parts use quoted-printable (not base64)."""
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText("Hello world", "plain", _charset=_utf8_qp))
    msg.attach(MIMEText("<p>Hello world</p>", "html", _charset=_utf8_qp))

    raw = msg.as_string()

    # Should use quoted-printable, not base64
    assert "Content-Transfer-Encoding: quoted-printable" in raw
    assert "Content-Transfer-Encoding: base64" not in raw


def test_quoted_printable_encoding_non_ascii():
    """Non-ASCII content is properly encoded with quoted-printable."""
    part = MIMEText("Hej från Sverige! Åäö", "plain", _charset=_utf8_qp)

    raw = part.as_string()
    assert "Content-Transfer-Encoding: quoted-printable" in raw
    assert "Content-Transfer-Encoding: base64" not in raw
    # The non-ASCII chars should be QP-encoded (=XX format)
    assert "=C3" in raw  # UTF-8 encoding of Swedish chars


def test_ascii_subject_not_encoded():
    """ASCII subjects should not be RFC 2047 encoded."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Daily Update - 2026-02-16"

    raw = msg.as_string()
    assert "Subject: Daily Update - 2026-02-16" in raw
    assert "=?utf-8?" not in raw


# ---------------------------------------------------------------------------
# Address-header construction (regression: non-ASCII display name → undeliverable)
# ---------------------------------------------------------------------------


def test_format_address_header_ascii_name():
    """An ASCII ``Name <addr>`` keeps a parseable addr-spec."""
    value = _format_address_header("Recipient Name <recipient@example.com>")
    name, addr = parseaddr(value)
    assert addr == "recipient@example.com"


def test_format_address_header_non_ascii_name_keeps_addr_spec():
    """A non-ASCII display name must NOT swallow the addr-spec.

    Regression for the silent-non-delivery bug: assigning a raw
    ``"Recipient Name <recipient@example.com>"`` to an email header makes Python's
    serializer treat the WHOLE string as one non-ASCII phrase and RFC2047-encode
    it, producing a To: with no parseable ``<addr>`` → undeliverable.
    """
    value = _format_address_header("Recipient Name <recipient@example.com>")
    # The addr-spec must survive as a real, parseable address.
    name, addr = parseaddr(value)
    assert addr == "recipient@example.com", f"addr-spec lost: {value!r}"
    # The whole string must NOT be wrapped as a single encoded phrase.
    assert "<recipient@example.com>" in value, f"angle-addr missing: {value!r}"
    # Non-ASCII name must be RFC2047-encoded (not raw bytes in the header).
    assert value.isascii(), f"header not ASCII-safe: {value!r}"


def test_format_address_header_bare_address():
    """A bare address (no display name) is returned usable."""
    value = _format_address_header("recipient@example.com")
    _, addr = parseaddr(value)
    assert addr == "recipient@example.com"


def test_send_to_header_non_ascii_name_is_deliverable(tmp_path, monkeypatch):
    """End-to-end: AgentEmail.send must emit a deliverable To: header.

    Composes a reply to a non-ASCII display-name recipient, stubs out msmtp,
    and asserts the serialized To: header carries a parseable addr-spec.
    """
    from email import message_from_bytes
    from email.utils import getaddresses

    from gptmail import lib

    sent_payloads: list[bytes] = []

    class _FakeCompleted:
        returncode = 0
        stdout = b""
        stderr = b""

    def _fake_run(cmd, input=None, **kwargs):  # noqa: A002 - mirrors subprocess API
        sent_payloads.append(input)
        return _FakeCompleted()

    monkeypatch.setattr(lib.subprocess, "run", _fake_run)
    monkeypatch.setenv("EMAIL_SEND_ALLOWLIST", "*")  # test is about MIME encoding, not allowlist

    email_dir = tmp_path / "email"
    for subdir in ["inbox", "sent", "archive", "drafts", "filters"]:
        (email_dir / subdir).mkdir(parents=True, exist_ok=True)

    agent = lib.AgentEmail(str(tmp_path), own_email="sender@example.com")
    monkeypatch.setattr(agent, "_validate_msmtp_config", lambda: True)
    monkeypatch.setattr(agent, "_get_msmtp_account_for_address", lambda addr: "default")
    monkeypatch.setattr(agent.rate_limiter, "can_proceed", lambda: True)

    # Markdown body → exercises the MIMEMultipart To path.
    message_id = agent.compose(
        "Recipient Name <recipient@example.com>",
        "Re: hello",
        "Thanks for the note!",
    )
    agent.send(message_id)

    assert sent_payloads, "msmtp was never invoked"
    parsed = message_from_bytes(sent_payloads[0])
    to_header = parsed["To"]
    addrs = [addr for _, addr in getaddresses([to_header])]
    assert "recipient@example.com" in addrs, f"To header undeliverable: {to_header!r}"
