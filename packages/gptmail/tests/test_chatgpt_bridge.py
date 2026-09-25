"""Tests for the ChatGPT ↔ Bob MCP bridge."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gptmail.chatgpt_bridge import ChatGPTBridge, _session_to_mailbox
from gptmail.transport.agent import AgentTransport


class TestSessionToMailbox:
    """Unit tests for session-to-mailbox mapping."""

    def test_deterministic(self) -> None:
        """Same session ID always maps to the same mailbox."""
        session = "sess-abc-123"
        m1 = _session_to_mailbox(session)
        m2 = _session_to_mailbox(session)
        assert m1 == m2

    def test_prefix(self) -> None:
        """Mailbox names start with cgpt-."""
        m = _session_to_mailbox("any-session")
        assert m.startswith("cgpt-")

    def test_different_sessions_different_mailboxes(self) -> None:
        """Different session IDs map to different mailboxes."""
        m1 = _session_to_mailbox("sess-a")
        m2 = _session_to_mailbox("sess-b")
        assert m1 != m2

    def test_no_leak(self) -> None:
        """Raw session ID does not appear in mailbox name."""
        session = "my-secret-session-id"
        m = _session_to_mailbox(session)
        assert session not in m


class TestChatGPTBridge:
    """Integration tests for the bridge."""

    @pytest.fixture
    def tmp_msgs(self, tmp_path: Path) -> Path:
        """Temporary messages directory."""
        return tmp_path / "messages"

    @pytest.fixture
    def bridge(self, tmp_msgs: Path) -> ChatGPTBridge:
        """Bridge instance with no auth."""
        return ChatGPTBridge(messages_dir=tmp_msgs, token=None)

    @pytest.fixture
    def session_id(self) -> str:
        return "test-session-xyz"

    @pytest.fixture
    def bob_transport(self, tmp_msgs: Path, session_id: str) -> AgentTransport:
        """Transport for Bob in the test mailbox."""
        mailbox = _session_to_mailbox(session_id)
        return AgentTransport(
            messages_dir=tmp_msgs,
            self_name="bob",
            mailbox=mailbox,
            deliver=None,
        )

    @staticmethod
    def _parse_result(result: tuple) -> dict:
        """Unpack FastMCP call_tool result.

        call_tool returns (list[TextContent], dict) where the list's first
        element carries the JSON payload.
        """
        return json.loads(result[0][0].text)

    @pytest.mark.anyio
    async def test_bob_status_empty(self, bridge: ChatGPTBridge, session_id: str) -> None:
        """Status on an empty mailbox reports zero counts."""
        result = await bridge.mcp.call_tool("bob_status", {"session_id": session_id})
        data = self._parse_result(result)
        assert data["mailbox"] == _session_to_mailbox(session_id)
        assert data["inbox"] == 0
        assert data["outbox"] == 0
        assert data["pending_replies"] == 0
        assert data["has_unread"] is False

    @pytest.mark.anyio
    async def test_bob_replies_empty(self, bridge: ChatGPTBridge, session_id: str) -> None:
        """Replies on an empty mailbox returns empty list."""
        result = await bridge.mcp.call_tool("bob_replies", {"session_id": session_id, "limit": 5})
        data = self._parse_result(result)
        assert data["replies"] == []

    @pytest.mark.anyio
    async def test_bob_replies_single(
        self,
        bridge: ChatGPTBridge,
        session_id: str,
        bob_transport: AgentTransport,
    ) -> None:
        """A single reply from Bob is returned."""
        bob_transport.send(
            to="chatgpt",
            subject="Hello",
            content="Bob says hi",
        )

        result = await bridge.mcp.call_tool("bob_replies", {"session_id": session_id, "limit": 5})
        data = self._parse_result(result)
        assert len(data["replies"]) == 1
        assert data["replies"][0]["subject"] == "Hello"
        assert data["replies"][0]["body"] == "Bob says hi"

    @pytest.mark.anyio
    async def test_bob_replies_idempotent(
        self,
        bridge: ChatGPTBridge,
        session_id: str,
        bob_transport: AgentTransport,
    ) -> None:
        """Calling bob_replies twice does not duplicate the reply."""
        bob_transport.send(
            to="chatgpt",
            subject="Hello",
            content="Bob says hi",
        )

        # First call returns the reply
        r1 = await bridge.mcp.call_tool("bob_replies", {"session_id": session_id, "limit": 5})
        d1 = self._parse_result(r1)
        assert len(d1["replies"]) == 1

        # Second call returns empty
        r2 = await bridge.mcp.call_tool("bob_replies", {"session_id": session_id, "limit": 5})
        d2 = self._parse_result(r2)
        assert d2["replies"] == []

    @pytest.mark.anyio
    async def test_bob_replies_limit(
        self,
        bridge: ChatGPTBridge,
        session_id: str,
        bob_transport: AgentTransport,
    ) -> None:
        """Limit parameter caps the number of replies."""
        for i in range(5):
            bob_transport.send(
                to="chatgpt",
                subject=f"Msg {i}",
                content=f"Body {i}",
            )

        result = await bridge.mcp.call_tool("bob_replies", {"session_id": session_id, "limit": 2})
        data = self._parse_result(result)
        assert len(data["replies"]) == 2

    @pytest.mark.anyio
    async def test_bob_status_after_reply(
        self,
        bridge: ChatGPTBridge,
        session_id: str,
        bob_transport: AgentTransport,
    ) -> None:
        """Status reflects pending replies correctly."""
        bob_transport.send(
            to="chatgpt",
            subject="Hello",
            content="Bob says hi",
        )

        result = await bridge.mcp.call_tool("bob_status", {"session_id": session_id})
        data = self._parse_result(result)
        assert data["outbox"] == 1
        assert data["pending_replies"] == 1
        assert data["has_unread"] is True

    @pytest.mark.anyio
    async def test_isolation_between_sessions(
        self,
        bridge: ChatGPTBridge,
        bob_transport: AgentTransport,
        tmp_msgs: Path,
    ) -> None:
        """Replies for session A do not leak to session B."""
        session_a = "session-a"
        session_b = "session-b"

        # Send reply to session A's mailbox
        transport_a = AgentTransport(
            messages_dir=tmp_msgs,
            self_name="bob",
            mailbox=_session_to_mailbox(session_a),
            deliver=None,
        )
        transport_a.send(to="chatgpt", subject="For A", content="Only A")

        # Session B should see no replies
        result = await bridge.mcp.call_tool("bob_replies", {"session_id": session_b, "limit": 5})
        data = self._parse_result(result)
        assert data["replies"] == []

    def test_auth_disabled_without_token(self, tmp_msgs: Path) -> None:
        """Auth is disabled when no token is set."""
        bridge = ChatGPTBridge(messages_dir=tmp_msgs, token=None)
        # _auth_ok should return True when no token configured
        assert bridge._auth_ok  # type: ignore[attr-defined]
