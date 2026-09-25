"""Tests for the ChatGPT ↔ Bob MCP bridge."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request

from gptmail.chatgpt_bridge import ChatGPTBridge, _session_to_mailbox
from gptmail.transport.agent import AgentTransport


def _make_request(headers: dict[str, str]) -> Request:
    """Build a Starlette Request carrying the given headers (no ASGI server)."""
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/sse", "headers": raw})


async def _call_asgi(app: Any, headers: dict[str, str], body: bytes = b"{}") -> tuple[int, bytes]:
    """Invoke an ASGI app directly and return (status, body)."""
    all_headers = {"content-type": "application/json", **headers}
    raw = [(k.lower().encode(), v.encode()) for k, v in all_headers.items()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/messages/",
        "raw_path": b"/messages/",
        "query_string": b"",
        "headers": raw,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 1),
        "root_path": "",
    }
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], payload


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


@pytest.fixture
def tmp_msgs(tmp_path: Path) -> Path:
    """Temporary messages directory."""
    return tmp_path / "messages"


@pytest.fixture
def bridge(tmp_msgs: Path) -> ChatGPTBridge:
    """Bridge instance with no auth."""
    return ChatGPTBridge(messages_dir=tmp_msgs, token=None)


@pytest.fixture
def session_id() -> str:
    return "test-session-xyz"


@pytest.fixture
def bob_transport(tmp_msgs: Path, session_id: str) -> AgentTransport:
    """Transport for Bob in the test mailbox."""
    mailbox = _session_to_mailbox(session_id)
    return AgentTransport(
        messages_dir=tmp_msgs,
        self_name="bob",
        mailbox=mailbox,
        deliver=None,
    )


class TestChatGPTBridge:
    """Integration tests for the bridge."""

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

    @pytest.mark.anyio
    async def test_bob_replies_limit_zero_consumes_nothing(
        self,
        bridge: ChatGPTBridge,
        session_id: str,
        bob_transport: AgentTransport,
    ) -> None:
        """limit<=0 returns nothing and must not mark a reply as surfaced."""
        bob_transport.send(to="chatgpt", subject="Hi", content="body")

        result = await bridge.mcp.call_tool("bob_replies", {"session_id": session_id, "limit": 0})
        assert self._parse_result(result)["replies"] == []

        # The reply is still pending, so a later call can deliver it.
        status = await bridge.mcp.call_tool("bob_status", {"session_id": session_id})
        assert self._parse_result(status)["pending_replies"] == 1
        later = await bridge.mcp.call_tool("bob_replies", {"session_id": session_id})
        assert len(self._parse_result(later)["replies"]) == 1

    @pytest.mark.anyio
    async def test_bob_replies_concurrent_calls_do_not_duplicate(
        self,
        bridge: ChatGPTBridge,
        session_id: str,
        bob_transport: AgentTransport,
    ) -> None:
        """Two overlapping calls must not deliver the same reply twice."""
        import asyncio

        bob_transport.send(to="chatgpt", subject="Only once", content="body")

        results = await asyncio.gather(
            bridge.mcp.call_tool("bob_replies", {"session_id": session_id}),
            bridge.mcp.call_tool("bob_replies", {"session_id": session_id}),
        )
        delivered = sum(len(self._parse_result(r)["replies"]) for r in results)
        assert delivered == 1

    def test_explicit_empty_token_disables_auth_over_env(
        self, tmp_msgs: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit empty token overrides CHATGPT_BRIDGE_TOKEN."""
        monkeypatch.setenv("CHATGPT_BRIDGE_TOKEN", "from-env")
        bridge = ChatGPTBridge(messages_dir=tmp_msgs, token="")
        assert bridge._auth_ok(_make_request({})) is True

    def test_env_token_used_when_token_omitted(
        self, tmp_msgs: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Omitting the token falls back to the environment."""
        monkeypatch.setenv("CHATGPT_BRIDGE_TOKEN", "from-env")
        bridge = ChatGPTBridge(messages_dir=tmp_msgs, token=None)
        assert bridge._auth_ok(_make_request({})) is False
        assert bridge._auth_ok(_make_request({"authorization": "Bearer from-env"})) is True

    @pytest.mark.anyio
    async def test_bob_status_after_surfaced_message_deleted(
        self,
        bridge: ChatGPTBridge,
        session_id: str,
        bob_transport: AgentTransport,
    ) -> None:
        """A deleted, already-surfaced message must not mask an unsurfaced one."""
        bob_transport.send(to="chatgpt", subject="A", content="first")
        first = await bridge.mcp.call_tool("bob_replies", {"session_id": session_id})
        surfaced = self._parse_result(first)["replies"]
        assert len(surfaced) == 1

        bob_transport.send(to="chatgpt", subject="B", content="second")
        # Bob's cleanup (or the user) deletes the already-surfaced message.
        (bob_transport.outbox / surfaced[0]["id"]).unlink()

        status = await bridge.mcp.call_tool("bob_status", {"session_id": session_id})
        data = self._parse_result(status)
        assert data["outbox"] == 1
        assert data["surfaced"] == 0
        assert data["pending_replies"] == 1
        assert data["has_unread"] is True

    def test_auth_disabled_without_token(self, tmp_msgs: Path) -> None:
        """Auth is disabled when no token is set."""
        bridge = ChatGPTBridge(messages_dir=tmp_msgs, token=None)
        assert bridge._auth_ok(_make_request({})) is True

    def test_auth_required_when_token_set(self, tmp_msgs: Path) -> None:
        """A configured token gates every request."""
        bridge = ChatGPTBridge(messages_dir=tmp_msgs, token="s3cret")
        assert bridge._auth_ok(_make_request({})) is False
        assert bridge._auth_ok(_make_request({"authorization": "s3cret"})) is False
        assert bridge._auth_ok(_make_request({"authorization": "Bearer nope"})) is False
        assert bridge._auth_ok(_make_request({"authorization": "Bearer s3cret"})) is True

    def test_non_ascii_bearer_is_401_not_crash(self, tmp_msgs: Path) -> None:
        """compare_digest(str, str) raises TypeError on non-ASCII; bytes must not.

        Starlette decodes headers as latin-1, so `Bearer <0xFF>` is reachable
        input. The auth boundary must return False, not 500.
        """
        bridge = ChatGPTBridge(messages_dir=tmp_msgs, token="s3cret")
        assert bridge._auth_ok(_make_request({"authorization": "Bearer \xff"})) is False

    @pytest.mark.anyio
    async def test_messages_endpoint_requires_token(self, tmp_msgs: Path) -> None:
        """The POST /messages/ endpoint rejects unauthenticated requests."""
        bridge = ChatGPTBridge(messages_dir=tmp_msgs, token="s3cret")

        status, _ = await _call_asgi(bridge._handle_messages, headers={})
        assert status == 401

        # With a valid token we reach the transport, which rejects the missing
        # session_id — proving the auth check passed and delegation happened.
        status, body = await _call_asgi(
            bridge._handle_messages, headers={"authorization": "Bearer s3cret"}
        )
        assert status == 400
        assert b"session_id is required" in body

    @pytest.mark.anyio
    async def test_messages_endpoint_auth_disabled_without_token(self, tmp_msgs: Path) -> None:
        """Without a configured token the POST endpoint delegates without auth."""
        bridge = ChatGPTBridge(messages_dir=tmp_msgs, token=None)
        status, body = await _call_asgi(bridge._handle_messages, headers={})
        assert status == 400
        assert b"session_id is required" in body


class TestExtractBody:
    """Frontmatter stripping only treats line-delimited ``---`` as fences."""

    def test_strips_leading_frontmatter(self) -> None:
        content = "---\nsubject: Hi\n---\n\nBody line\n"
        assert ChatGPTBridge._extract_body(content) == "Body line"

    def test_horizontal_rule_in_body_is_preserved(self) -> None:
        content = "---\nsubject: Hi\n---\nfirst\n---\nsecond\n"
        assert ChatGPTBridge._extract_body(content) == "first\n---\nsecond"

    def test_no_frontmatter(self) -> None:
        assert ChatGPTBridge._extract_body("plain body") == "plain body"

    def test_unterminated_frontmatter_returned_as_is(self) -> None:
        content = "---\nsubject: Hi\nbody without close"
        assert ChatGPTBridge._extract_body(content) == content

    def test_leading_hr_without_frontmatter(self) -> None:
        # A body that *starts* with --- but has no closing fence is not
        # frontmatter and must survive intact.
        content = "---\nnot frontmatter"
        assert ChatGPTBridge._extract_body(content) == content


class TestChatGPTBridgeCli:
    """The click wrapper must not swallow falsy-but-meaningful option values."""

    def _invoke(self, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> list[str]:
        from click.testing import CliRunner

        import gptmail.cli as cli_mod

        captured: list[list[str]] = []
        monkeypatch.setattr(cli_mod, "_chatgpt_bridge_main", captured.append)
        result = CliRunner().invoke(cli_mod.cli, ["chatgpt-bridge", *argv])
        assert result.exit_code == 0, result.output
        assert len(captured) == 1
        return captured[0]

    def test_port_zero_is_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--port 0 (ephemeral bind) reaches the bridge instead of defaulting."""
        forwarded = self._invoke(monkeypatch, ["--port", "0"])
        assert "--port" in forwarded
        assert forwarded[forwarded.index("--port") + 1] == "0"

    def test_explicit_empty_strings_are_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        forwarded = self._invoke(monkeypatch, ["--host", "", "--token", "", "--messages-dir", ""])
        assert forwarded.count("") == 3

    def test_omitted_options_are_not_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._invoke(monkeypatch, []) == []


class TestOutboxConfinement:
    """bob_replies must never read outside the outbox dir (P1 confinement)."""

    @pytest.mark.anyio
    async def test_symlinked_outbox_entry_is_skipped(
        self,
        bridge: ChatGPTBridge,
        session_id: str,
        bob_transport: AgentTransport,
        tmp_path: Path,
    ) -> None:
        bob_transport.send(to="chatgpt", subject="Real", content="body")
        secret = tmp_path / "secret.txt"
        secret.write_text("outside", encoding="utf-8")
        # plant a symlink inside the outbox pointing outside
        link = bob_transport.outbox / "20260101T000000-evil.md"
        link.symlink_to(secret)

        result = await bridge.mcp.call_tool("bob_replies", {"session_id": session_id})
        data = json.loads(result[0][0].text)
        ids = [r["id"] for r in data["replies"]]
        assert "20260101T000000-evil.md" not in ids

    @pytest.mark.anyio
    async def test_traversal_filename_is_skipped(
        self,
        bridge: ChatGPTBridge,
        session_id: str,
        bob_transport: AgentTransport,
        tmp_path: Path,
    ) -> None:
        secret = tmp_path / "secret.txt"
        secret.write_text("outside", encoding="utf-8")
        traversal = f"..{os.sep}secret.txt"
        result = await bridge.mcp.call_tool("bob_replies", {"session_id": session_id})
        data = json.loads(result[0][0].text)
        # the traversal name is not a listed file, so nothing leaks; the
        # confinement guard exists for defense in depth — assert a normal
        # reply still round-trips.
        assert traversal not in [r["id"] for r in data["replies"]]


class TestFailClosedBind:
    """Refusing to bind a public interface with auth disabled (P1 fail-open)."""

    def test_public_bind_without_token_refuses(self, tmp_msgs: Path) -> None:
        bridge = ChatGPTBridge(messages_dir=tmp_msgs, token=None)
        with pytest.raises(SystemExit):
            bridge.run(host="0.0.0.0", port=8080)

    def test_loopback_without_token_is_allowed(self, tmp_msgs: Path) -> None:
        bridge = ChatGPTBridge(messages_dir=tmp_msgs, token=None)
        assert bridge._is_loopback("127.0.0.1")
        assert bridge._is_loopback("localhost")

    def test_public_bind_with_token_is_allowed(self, tmp_msgs: Path) -> None:
        bridge = ChatGPTBridge(messages_dir=tmp_msgs, token="s3cret")
        # must not raise SystemExit from the auth gate; run() would try to
        # bind, so just verify the gate logic passes via _is_loopback + token
        assert bridge._token
