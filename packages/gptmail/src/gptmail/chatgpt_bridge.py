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
import json
import logging
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

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
        self._token = token or os.environ.get("CHATGPT_BRIDGE_TOKEN")
        self._surfaced: _SurfacedLedger = defaultdict(set)

        # FastMCP server — tools registered below
        self.mcp = FastMCP("bob-chatgpt-bridge")
        self._register_tools()

        # SSE transport wrapping the FastMCP server
        self._sse = SseServerTransport("/messages/")

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------
    def _auth_ok(self, request: Request) -> bool:
        if not self._token:
            return True  # auth disabled (dev only)
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return False
        return auth[7:].strip() == self._token

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
            outbox_count = len(transport.list_inbox("outbox"))
            surfaced_count = len(self._surfaced.get(session_id, set()))

            pending = outbox_count - surfaced_count
            status = {
                "mailbox": mailbox,
                "inbox": inbox_count,
                "outbox": outbox_count,
                "surfaced": surfaced_count,
                "pending_replies": max(0, pending),
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
            surfaced = self._surfaced[session_id]

            replies: list[dict[str, Any]] = []
            # Read outbox (messages FROM Bob TO chatgpt).
            # transport.read() is inbox-only, so we read the file directly.
            for msg_id, subject, _ts in transport.list_inbox("outbox"):
                if msg_id in surfaced:
                    continue
                path = transport.outbox / msg_id
                if not path.exists():
                    continue
                content = path.read_text()
                body = self._extract_body(content)
                replies.append(
                    {
                        "id": msg_id,
                        "subject": subject,
                        "body": body,
                    }
                )
                surfaced.add(msg_id)
                if len(replies) >= limit:
                    break

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
        """Strip YAML frontmatter and return the markdown body."""
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                return parts[2].strip()
        return content.strip()

    # ------------------------------------------------------------------
    # Starlette app (SSE endpoint + health)
    # ------------------------------------------------------------------
    def _create_app(self) -> Starlette:
        async def handle_sse(request: Request) -> None:
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

        async def health(request: Request) -> JSONResponse:
            return JSONResponse({"status": "ok", "bridge": "bob-chatgpt-bridge"})

        return Starlette(
            debug=False,
            routes=[
                Route("/health", health),
                Route("/sse", endpoint=handle_sse),
                Mount("/messages/", app=self._sse.handle_post_message),
            ],
        )

    def run(self, host: str = "127.0.0.1", port: int = 8080) -> None:
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
        default=int(os.environ.get("CHATGPT_BRIDGE_PORT", "8080")),
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
