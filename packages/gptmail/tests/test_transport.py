"""Tests for the transport seam.

Step 1 of folding agent-msg into gptmail: a ``Transport`` Protocol plus an
``EmailTransport`` adapter over ``lib.AgentEmail``. These tests exercise the
seam without touching the network — ``send`` is verified by monkeypatching the
underlying delivery so no msmtp/SMTP call is made.

See task: fold-agent-msg-into-gptmail-single-comms-tool.
"""

from datetime import datetime
from pathlib import Path

from gptmail.transport import EmailTransport, Transport


def _make_transport(tmp_path: Path) -> EmailTransport:
    email_dir = tmp_path / "email"
    for subdir in ["inbox", "sent", "archive", "drafts", "filters"]:
        (email_dir / subdir).mkdir(parents=True, exist_ok=True)
    return EmailTransport(tmp_path, own_email="alice@example.com")


def test_email_transport_satisfies_protocol(tmp_path: Path) -> None:
    transport = _make_transport(tmp_path)
    # runtime_checkable Protocol — structural conformance.
    assert isinstance(transport, Transport)


def test_channel_is_email(tmp_path: Path) -> None:
    assert _make_transport(tmp_path).channel == "email"


def test_conversation_id_is_constant(tmp_path: Path) -> None:
    """Email shares a single tracker conversation, matching AgentEmail."""
    transport = _make_transport(tmp_path)
    assert transport.conversation_id_for("any-id") == "email"


def test_send_delegates_to_compose_and_deliver(tmp_path: Path, monkeypatch) -> None:
    """send() composes a draft then delivers it, returning the message id.

    Delivery (msmtp) is stubbed so the test stays offline.
    """
    transport = _make_transport(tmp_path)
    delivered: list[str] = []
    monkeypatch.setattr(transport.email, "send", lambda mid: delivered.append(mid))

    message_id = transport.send("bob@example.com", "Hi", "Hello Bob")

    assert message_id
    assert delivered == [message_id]
    # The draft was actually composed on disk before delivery.
    drafts = list((tmp_path / "email" / "drafts").glob("*"))
    assert drafts, "expected a composed draft file"


def test_list_and_read_delegate(tmp_path: Path, monkeypatch) -> None:
    transport = _make_transport(tmp_path)
    sentinel_list: list[tuple[str, str, datetime]] = [("m1", "Subject", datetime(2026, 6, 13))]
    monkeypatch.setattr(transport.email, "list_messages", lambda folder="inbox": sentinel_list)
    monkeypatch.setattr(
        transport.email,
        "read_message",
        lambda mid, include_thread=False: f"body:{mid}:{include_thread}",
    )

    assert transport.list_inbox() == sentinel_list
    assert transport.read("m1", include_thread=True) == "body:m1:True"
