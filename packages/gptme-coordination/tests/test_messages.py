"""Tests for the append-only message bus."""

from pathlib import Path

import pytest
from gptme_coordination.db import CoordinationDB
from gptme_coordination.messages import MessageBus


@pytest.fixture
def db(tmp_path: Path) -> CoordinationDB:
    return CoordinationDB(tmp_path / "test.db")


@pytest.fixture
def bus(db: CoordinationDB) -> MessageBus:
    return MessageBus(db)


class TestSendMessage:
    def test_send_broadcast(self, bus: MessageBus) -> None:
        msg = bus.send("agent-1", "found bug in parser.py")
        assert msg.sender == "agent-1"
        assert msg.recipient is None
        assert msg.channel == "general"
        assert msg.body == "found bug in parser.py"
        assert msg.id > 0

    def test_send_targeted(self, bus: MessageBus) -> None:
        msg = bus.send("agent-1", "hello", recipient="agent-2")
        assert msg.recipient == "agent-2"

    def test_send_with_channel(self, bus: MessageBus) -> None:
        msg = bus.send("agent-1", "active", channel="announce")
        assert msg.channel == "announce"


class TestInbox:
    def test_inbox_sees_broadcast(self, bus: MessageBus) -> None:
        bus.send("agent-1", "broadcast message")
        msgs = bus.inbox("agent-2")
        assert len(msgs) == 1
        assert msgs[0].body == "broadcast message"

    def test_inbox_sees_targeted(self, bus: MessageBus) -> None:
        bus.send("agent-1", "hello", recipient="agent-2")
        msgs = bus.inbox("agent-2")
        assert len(msgs) == 1

    def test_inbox_excludes_other_targeted(self, bus: MessageBus) -> None:
        bus.send("agent-1", "only for agent-3", recipient="agent-3")
        msgs = bus.inbox("agent-2")
        assert len(msgs) == 0

    def test_inbox_channel_filter(self, bus: MessageBus) -> None:
        bus.send("agent-1", "general msg")
        bus.send("agent-1", "announce msg", channel="announce")
        msgs = bus.inbox("agent-2", channel="announce")
        assert len(msgs) == 1
        assert msgs[0].channel == "announce"


class TestHistory:
    def test_history_returns_recent(self, bus: MessageBus) -> None:
        bus.send("a", "msg1")
        bus.send("a", "msg2")
        history = bus.history(limit=10)
        assert len(history) == 2

    def test_history_channel_filter(self, bus: MessageBus) -> None:
        bus.send("a", "general", channel="general")
        bus.send("a", "announce", channel="announce")
        history = bus.history(channel="announce")
        assert len(history) == 1
        assert history[0].body == "announce"


class TestHMACVerification:
    def test_verified_false_without_secrets(self, bus: MessageBus) -> None:
        """Messages with a stored HMAC are NOT verified until inbox() compares against a secret."""
        secret = b"s3cr3t"
        bus.send("agent-1", "hello", secret=secret)
        # history() and inbox() without secrets must not claim verified=True
        history = bus.history()
        assert len(history) == 1
        assert history[0].hmac is not None, "HMAC should be stored"
        assert history[0].verified is False, "stored HMAC ≠ verified"

    def test_inbox_without_secrets_not_verified(self, bus: MessageBus) -> None:
        """inbox() without a secrets dict must leave verified=False on HMAC'd messages."""
        bus.send("agent-1", "hi", recipient="agent-2", secret=b"s3cr3t")
        msgs = bus.inbox("agent-2")
        assert msgs[0].verified is False

    def test_inbox_with_correct_secret_verified(self, bus: MessageBus) -> None:
        """inbox() with the correct secret marks the message verified=True."""
        secret = b"s3cr3t"
        bus.send("agent-1", "hi", recipient="agent-2", secret=secret)
        msgs = bus.inbox("agent-2", secrets={"agent-1": secret})
        assert msgs[0].verified is True

    def test_inbox_with_wrong_secret_not_verified(self, bus: MessageBus) -> None:
        """inbox() with the wrong secret marks the message verified=False."""
        bus.send("agent-1", "hi", recipient="agent-2", secret=b"correct")
        msgs = bus.inbox("agent-2", secrets={"agent-1": b"wrong"})
        assert msgs[0].verified is False

    def test_no_hmac_message_not_verified(self, bus: MessageBus) -> None:
        """Messages sent without a secret have no HMAC and must be verified=False."""
        bus.send("agent-1", "unauthenticated")
        history = bus.history()
        assert history[0].hmac is None
        assert history[0].verified is False
