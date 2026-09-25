"""ChatGPT ↔ Bob bridge — remote MCP server over HTTP/SSE.

Maps each ChatGPT chat (identified by ``_meta["openai/session"]``) to a gptmail
mailbox ``cgpt-<hash>``. Exposes read-only tools so ChatGPT can pull Bob's
replies from the filesystem outbox.

Usage::

    python -m gptmail.chatgpt_bridge --host 0.0.0.0 --port 8080

Or via the CLI wrapper::

    gptmail chatgpt-bridge --host 0.0.0.0 --port 8080

The server requires a ``CHATGPT_BRIDGE_TOKEN`` environment variable for bearer
token auth (OpenAI remote MCP sends this on every request).
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import sys
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

from gptmail.transport.agent import AgentTransport

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Surface ledger: which message IDs have already been returned per session.
# In-memory only; restarting the server will re-surface.  For production
# durability, swap this for a tiny SQLite table or append-only JSONL file.
# ---------------------------------------------------------------------------
_SurfacedLedger = dict[str, set[str]]


def _session_to_mailbox(session_id: str) -> str:
    """Map an OpenAI session ID to a gptmail mailbox name.

    The mailbox name is deterministic so the same chat always reads from the
    same inbox, but hashed so the raw session ID never leaks into filenames.
    """
    h = hashlib.sha256(session_id.encode()).hexdigest()[:16]
    return f"cgpt-{h}"


class ChatGPTBridge:
    """MCP bridge: ChatGPT chats → gptmail mailboxes."""

    def __init__(
        self,
        messages_dir: str | Path,
        self_name: str = "chatgpt",
        *,
        token: str | None = None,
    ) -> None:
        self._messages_dir = Path(messages_dir)
        self._self_name = self_name.lower()
        # `is not None`, not truthiness: an explicit empty --token means
        # "disable auth" and must override a token in the environment.
        self._token = token if token is not None else os.environ.get("CHATGPT_BRIDGE_TOKEN")
        self._surfaced: _SurfacedLedger = defaultdict(set)
        # One lock per session: Tool calls may run concurrently in FastMCP's
        # threadpool, and the surfaced check-and-add must be atomic so the same
        # reply is not delivered to two simultaneous callers.
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

        # FastMCP server — tools registered below
        self.mcp = FastMCP("bob-chatgpt-bridge")
        self._register_tools()

        # SSE transport wrapping the FastMCP server
        self._sse = SseServerTransport("/messages/")

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------
    def _auth_ok(self, request: Request) -> bool:
        return self._auth_ok_headers(request.headers)

    def _session_lock(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[session_id] = lock
            return lock

    def _auth_ok_headers(self, headers: Headers) -> bool:
        if not self._token:
            return True  # auth disabled (dev only)
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return False
        # constant-time compare: a plain == leaks the token byte-by-byte to a
        # timing attacker who can reach the port. Compared as UTF-8 bytes:
        # secrets.compare_digest(str, str) raises TypeError on non-ASCII
        # input (Starlette decodes headers as latin-1, so the credential is
        # attacker-controlled), while bytes input never raises — a malformed
        # header gets a clean 401 instead of a 500 traceback.
        return secrets.compare_digest(
            auth[7:].strip().encode("utf-8"),
            self._token.encode("utf-8"),
        )

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------
    def _register_tools(self) -> None:
        @self.mcp.tool()
        def bob_status(session_id: str) -> str:
            """Return Bob's status for this chat session.

            Args:
                session_id: The OpenAI session ID (from _meta["openai/session"]).
            """
            mailbox = _session_to_mailbox(session_id)
            transport = self._transport(mailbox)
            inbox_count = len(transport.list_inbox("inbox"))
            outbox_ids = {msg_id for msg_id, _subject, _ts in transport.list_inbox("outbox")}
            with self._session_lock(session_id):
                surfaced = set(self._surfaced.get(session_id, set()))

            # Compare against the outbox *contents*, not a running subtraction:
            # a surfaced message that is later deleted from the outbox must not
            # mask a reply that was never surfaced (it would undercount pending
            # and report has_unread=False while unread replies remain).
            pending = len(outbox_ids - surfaced)
            status = {
                "mailbox": mailbox,
                "inbox": inbox_count,
                "outbox": len(outbox_ids),
                "surfaced": len(outbox_ids & surfaced),
                "pending_replies": pending,
                "has_unread": pending > 0,
            }
            return json.dumps(status, indent=2)

        @self.mcp.tool()
        def bob_replies(session_id: str, limit: int = 5) -> str:
            """Return unread Bob replies for this chat session.

            Args:
                session_id: The OpenAI session ID (from _meta["openai/session"]).
                limit: Maximum number of replies to return (default 5).
            """
            mailbox = _session_to_mailbox(session_id)
            transport = self._transport(mailbox)

            replies: list[dict[str, Any]] = []
            # Hold the per-session lock across the whole check-and-add: two
            # concurrent calls must not both read an unsurfaced message and
            # deliver it twice.
            with self._session_lock(session_id):
                surfaced = self._surfaced[session_id]
                # Read outbox (messages FROM Bob TO chatgpt). transport.read()
                # is inbox-only, so we read the file directly.
                for msg_id, subject, _ts in transport.list_inbox("outbox"):
                    # check the cap before reading/marking: with limit<=0 no
                    # message may be consumed (a reply marked surfaced here
                    # would never be delivered again).
                    if len(replies) >= limit:
                        break
                    if msg_id in surfaced:
                        continue
                    path = transport.outbox / msg_id
                    # confinement: msg_id is a filename from a directory
                    # listing, but if anything ever plants a crafted name
                    # (traversal component, symlink) the read must not leave
                    # the outbox.
                    resolved = path.resolve()
                    if path.is_symlink() or not resolved.is_relative_to(transport.outbox.resolve()):
                        continue
                    try:
                        # O_NOFOLLOW closes the check-to-read symlink swap
                        # (TOCTOU): the open fails on a symlinked final
                        # component instead of following it.
                        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    except OSError:
                        continue
                    try:
                        with os.fdopen(fd, "rb") as f:
                            raw = f.read()
                        # decode bytes directly: a file that was concurrently
                        # deleted or holds invalid UTF-8 must not break the
                        # whole call for every other reply in the session.
                        content = raw.decode("utf-8", errors="replace")
                    except OSError:
                        continue
                    body = self._extract_body(content)
                    replies.append(
                        {
                            "id": msg_id,
                            "subject": subject,
                            "body": body,
                        }
                    )
                    surfaced.add(msg_id)

            if not replies:
                return json.dumps(
                    {"replies": [], "note": "No new replies from Bob."},
                    indent=2,
                )

            return json.dumps({"replies": replies}, indent=2)

    # ------------------------------------------------------------------
    # Transport helpers
    # ------------------------------------------------------------------
    def _transport(self, mailbox: str) -> AgentTransport:
        return AgentTransport(
            messages_dir=self._messages_dir,
            self_name=self._self_name,
            mailbox=mailbox,
            deliver=None,  # local-only; no SSH delivery needed for reading
        )

    @staticmethod
    def _extract_body(content: str) -> str:
        """Strip a leading YAML frontmatter block and return the markdown body.

        Only the *line-delimited* ``---`` fences count as frontmatter
        delimiters, so a ``---`` horizontal rule inside the body (or inside a
        frontmatter string) is left intact.
        """
        lines = content.splitlines()
        if lines and lines[0].strip() == "---":
            for i in range(1, len(lines)):
                if lines[i].strip() == "---":
                    return "\n".join(lines[i + 1 :]).strip()
        return content.strip()

    # ------------------------------------------------------------------
    # Starlette app (SSE endpoint + health)
    # ------------------------------------------------------------------
    async def _handle_messages(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI wrapper around the SSE transport's POST endpoint.

        ``SseServerTransport.handle_post_message`` is only usable with a
        ``session_id`` issued over the bearer-authenticated ``/sse`` handshake,
        so it is not world-callable on its own. The bearer token is still
        enforced here explicitly: defence in depth, and it keeps the auth
        boundary in one visible place instead of resting on an SDK-internal
        capability for its security.
        """
        request = Request(scope, receive)
        if not self._auth_ok(request):
            response = JSONResponse({"error": "Unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self._sse.handle_post_message(scope, receive, send)

    def _create_app(self) -> Starlette:
        async def handle_sse(request: Request) -> Response:
            if not self._auth_ok(request):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            async with self._sse.connect_sse(request.scope, request.receive, request._send) as (
                read_stream,
                write_stream,
            ):
                await self.mcp._mcp_server.run(
                    read_stream,
                    write_stream,
                    self.mcp._mcp_server.create_initialization_options(),
                )
            # The SSE response was already written via request._send; Starlette
            # still expects a Response object from the endpoint.
            return Response()

        async def health(request: Request) -> JSONResponse:
            return JSONResponse({"status": "ok", "bridge": "bob-chatgpt-bridge"})

        return Starlette(
            debug=False,
            routes=[
                Route("/health", health),
                Route("/sse", endpoint=handle_sse),
                Mount("/messages/", app=self._handle_messages),
            ],
        )

    @staticmethod
    def _is_loopback(host: str) -> bool:
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False  # unknown host forms are treated as public

    def run(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        # fail closed: binding a non-loopback interface with auth disabled
        # exposes the mailbox to the local network. Dev usage on loopback
        # (the default) is unaffected.
        if not self._token and not self._is_loopback(host):
            raise SystemExit(
                "chatgpt-bridge: refusing to bind %s without CHATGPT_BRIDGE_TOKEN "
                "(auth would be disabled on a public interface). Set a token or "
                "bind to 127.0.0.1." % host
            )
        """Start the bridge server."""
        import uvicorn

        app = self._create_app()
        logger.info("Starting bob-chatgpt-bridge on %s:%d", host, port)
        uvicorn.run(app, host=host, port=port, log_level="info")


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description="ChatGPT ↔ Bob MCP bridge")
    parser.add_argument(
        "--host",
        default=os.environ.get("CHATGPT_BRIDGE_HOST", "127.0.0.1"),
        help="Host to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        # String default: argparse applies type conversion to string defaults,
        # so a malformed CHATGPT_BRIDGE_PORT gets argparse's clean usage error
        # instead of an eager int() ValueError traceback.
        default=os.environ.get("CHATGPT_BRIDGE_PORT", "8080"),
        help="Port to bind (default: 8080)",
    )
    parser.add_argument(
        "--messages-dir",
        default=os.environ.get("CHATGPT_BRIDGE_MESSAGES_DIR"),
        help="Path to gptmail messages directory (default: auto-detect from git)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("CHATGPT_BRIDGE_TOKEN"),
        help="Bearer token for auth (default: CHATGPT_BRIDGE_TOKEN env var)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Auto-detect messages directory if not provided
    messages_dir = args.messages_dir
    if not messages_dir:
        try:
            import subprocess

            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True,
            )
            messages_dir = str(Path(result.stdout.strip()) / "messages")
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.error(
                "Could not auto-detect messages directory. "
                "Set --messages-dir or CHATGPT_BRIDGE_MESSAGES_DIR."
            )
            sys.exit(1)

    bridge = ChatGPTBridge(
        messages_dir=messages_dir,
        token=args.token,
    )
    bridge.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
