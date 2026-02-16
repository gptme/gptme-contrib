"""Tests for MIME email construction in gptmail."""

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from gptmail.lib import _is_html, _utf8_qp


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
