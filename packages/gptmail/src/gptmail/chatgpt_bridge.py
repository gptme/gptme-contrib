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
import contextlib
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import sys
import threading
from collections import defaultdict
from collections.abc import Iterator
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

from gptmail.transport.agent import AgentTransport, split_frontmatter

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


# Cap on sessions whose lock + surfaced ledger are retained. Tool calls are
# low-frequency (chat polling), so this only ever evicts long-idle sessions.
_MAX_TRACKED_SESSIONS = 1024


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
        if self._token is not None:
            # A token sourced from an env var or file routinely carries a
            # surrounding newline, which an HTTP header cannot: the header
            # side is stripped before comparison, so an unstripped token would
            # reject every otherwise-valid request with a 401. Normalize once
            # here rather than at every comparison.
            stripped = self._token.strip()
            if not stripped and self._token:
                # Fail closed: a whitespace-only token is a config error, not
                # a request to disable auth (only an explicit "" means that).
                raise ValueError(
                    "chatgpt-bridge: token is blank (whitespace only); "
                    "pass an empty string to disable auth explicitly"
                )
            self._token = stripped
        self._surfaced: _SurfacedLedger = defaultdict(set)
        # One lock per session: Tool calls may run concurrently in FastMCP's
        # threadpool, and the surfaced check-and-add must be atomic so the same
        # reply is not delivered to two simultaneous callers.
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # Sessions with a thread that has been handed the lock and has not yet
        # left the critical section. Eviction must never reclaim one of these:
        # a lock picked up but not yet acquired is invisible to `.locked()`, and
        # dropping it would let a second caller create a fresh lock for the same
        # session — two threads then hold different locks and race on the
        # surfaced ledger (duplicate delivery).
        self._in_flight: dict[str, int] = {}

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

    @contextlib.contextmanager
    def _session_lock(self, session_id: str) -> Iterator[None]:
        """Hold this session's lock across the check-and-add critical section.

        The lock reference and the in-flight registration are taken under
        ``_locks_guard``, *before* the lock is acquired, so eviction can never
        run between the two: a lock that has been handed to a caller is always
        counted in ``_in_flight`` until that caller leaves the section.
        """
        with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                self._evict_idle_sessions()
                lock = threading.Lock()
                self._locks[session_id] = lock
            self._in_flight[session_id] = self._in_flight.get(session_id, 0) + 1
        try:
            with lock:
                yield
        finally:
            with self._locks_guard:
                remaining = self._in_flight[session_id] - 1
                if remaining:
                    self._in_flight[session_id] = remaining
                else:
                    del self._in_flight[session_id]

    def _evict_idle_sessions(self) -> None:
        """Keep the per-session maps bounded on a long-lived server.

        Caller must hold ``self._locks_guard``. Only sessions with no in-flight
        caller, an unheld lock, and no live surfaced ids are evicted: dropping a
        lock that is in use would reopen the check-and-add race the lock exists
        for, and a non-empty ledger still gates re-delivery. ``locked()`` is
        kept alongside ``_in_flight`` so a lock handed out by a test (or any
        holder not registered through ``_session_lock``) is still respected.
        """
        if len(self._locks) < _MAX_TRACKED_SESSIONS:
            return
        for session_id in list(self._locks):
            if len(self._locks) < _MAX_TRACKED_SESSIONS:
                break
            if (
                self._in_flight.get(session_id)
                or self._locks[session_id].locked()
                or self._surfaced.get(session_id)
            ):
                continue
            del self._locks[session_id]
            self._surfaced.pop(session_id, None)

    def _auth_ok_headers(self, headers: Headers) -> bool:
        if not self._token:
            return True  # auth disabled (dev only)
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return False
        # constant-time compare: a plain == leaks the token byte-by-byte to a
        # timing attacker who can reach the port. Compared as bytes:
        # secrets.compare_digest(str, str) raises TypeError on non-ASCII
        # input (Starlette decodes headers as latin-1, so the credential is
        # attacker-controlled), while bytes input never raises — a malformed
        # header gets a clean 401 instead of a 500 traceback.
        # The header side is re-encoded as latin-1, not UTF-8: Starlette
        # decoded the raw header bytes as latin-1, so latin-1 is the exact
        # inverse and recovers the bytes the client actually sent (a UTF-8
        # token). Encoding the decoded string as UTF-8 instead would mangle
        # every non-ASCII byte (`é` → `Ã©`), rejecting a valid non-ASCII
        # token forever. latin-1 is total over latin-1-decoded strings, so
        # this cannot raise either.
        return secrets.compare_digest(
            auth[7:].strip().encode("latin-1"),
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
                # Prune ids whose outbox message is gone: they can never be
                # delivered again, and retaining them would keep the session
                # un-evictable (the sweep skips non-empty ledgers). This does
                # not change `pending`: ids outside the outbox were already
                # excluded by `outbox_ids - surfaced`.
                ledger = self._surfaced.get(session_id)
                if ledger is not None:
                    ledger.intersection_update(outbox_ids)
                surfaced = set(ledger or ())

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
            # msg_ids collected by this call; committed to the surfaced ledger
            # only after the payload is built (see below).
            newly_surfaced: list[str] = []
            # Hold the per-session lock across the whole check-and-add: two
            # concurrent calls must not both read an unsurfaced message and
            # deliver it twice.
            with self._session_lock(session_id):
                surfaced = self._surfaced[session_id]
                # Read outbox (messages FROM Bob TO chatgpt). transport.read()
                # is inbox-only, so we read the file directly. Materialize the
                # listing so the ledger can be pruned against the whole outbox
                # before the loop (a session that only ever polls bob_replies
                # never runs bob_status' flush, and a stale id both misleads
                # nothing and keeps the session un-evictable).
                entries = list(transport.list_inbox("outbox"))
                surfaced.intersection_update(mid for mid, _s, _t in entries)

                # Open the outbox directory itself with O_NOFOLLOW. A symlinked
                # outbox would otherwise be followed, and the containment check
                # below resolves *through* it — so that check passes while reads
                # land wherever the link points. O_DIRECTORY|O_NOFOLLOW fails
                # closed (ELOOP) instead, and the file is then opened relative to
                # this fd rather than by path.
                outbox_fd: int | None = None
                if entries:
                    try:
                        outbox_fd = os.open(
                            transport.outbox,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        )
                    except OSError:
                        logger.warning(
                            "chatgpt-bridge: refusing to read outbox %s (symlinked or unreadable)",
                            transport.outbox,
                        )
                        return json.dumps(
                            {"replies": [], "note": "No new replies from Bob."},
                            indent=2,
                        )
                try:
                    for msg_id, subject, _ts in entries:
                        # The cap counts *collected* replies, not iterations, so
                        # already-surfaced messages never consume it. Checked
                        # before reading/marking: with limit<=0 no message may be
                        # consumed (a reply marked surfaced here would never be
                        # delivered again).
                        if len(replies) >= limit:
                            break
                        if msg_id in surfaced:
                            continue
                        path = transport.outbox / msg_id
                        # confinement: msg_id is a filename from a directory
                        # listing, but if anything ever plants a crafted name
                        # (traversal component, symlink) the read must not
                        # leave the outbox. Still needed alongside dir_fd:
                        # a ".."-bearing relative name traverses from the fd.
                        resolved = path.resolve()
                        if path.is_symlink() or not resolved.is_relative_to(
                            transport.outbox.resolve()
                        ):
                            continue
                        try:
                            # O_NOFOLLOW closes the check-to-read symlink swap
                            # (TOCTOU): the open fails on a symlinked final
                            # component instead of following it.
                            fd = os.open(
                                msg_id,
                                os.O_RDONLY | os.O_NOFOLLOW,
                                dir_fd=outbox_fd,
                            )
                        except OSError:
                            continue
                        try:
                            with os.fdopen(fd, "rb") as f:
                                raw = f.read()
                            # decode bytes directly: a file that was
                            # concurrently deleted or holds invalid UTF-8 must
                            # not break the whole call for every other reply in
                            # the session.
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
                        newly_surfaced.append(msg_id)
                finally:
                    if outbox_fd is not None:
                        os.close(outbox_fd)

                # The commit to the ledger stays inside the lock: moving it
                # outside would reopen the check-and-add race the lock exists
                # for. It is still deferred until the payload is built, so a
                # failure partway through the call cannot consume replies that
                # were collected but never returned. Delivery to the client
                # happens after this function returns and is not observable
                # here; the outbox on disk remains the source of truth, so a
                # response dropped in that window is recoverable.
                if not replies:
                    return json.dumps(
                        {"replies": [], "note": "No new replies from Bob."},
                        indent=2,
                    )

                payload = json.dumps({"replies": replies}, indent=2)
                surfaced.update(newly_surfaced)
                return payload

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

        Uses the transport's shared ``split_frontmatter`` rule: a leading
        ``---`` is frontmatter only when a closing fence exists *and* the block
        between them is a YAML mapping. So a body that merely starts with a
        ``---`` horizontal rule — with or without a second one — survives intact,
        matching how ``meta_of`` reads the same files.
        """
        return split_frontmatter(content)[1].strip()

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

    def _bind_allowed(self, host: str) -> bool:
        """Whether the auth gate lets us bind ``host``.

        Fail closed: anything we cannot prove is loopback (including ``""``,
        ``0.0.0.0``, ``::`` and unknown hostnames) requires a token. Kept as a
        separate method so the gate is testable without opening a socket.
        """
        return bool(self._token) or self._is_loopback(host)

    def run(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        # fail closed: binding a non-loopback interface with auth disabled
        # exposes the mailbox to the local network. Dev usage on loopback
        # (the default) is unaffected.
        if not self._bind_allowed(host):
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

    try:
        bridge = ChatGPTBridge(
            messages_dir=messages_dir,
            token=args.token,
        )
    except ValueError as exc:
        # e.g. a whitespace-only --token / CHATGPT_BRIDGE_TOKEN: a clean usage
        # error beats a traceback from the constructor.
        logger.error("%s", exc)
        sys.exit(2)
    bridge.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
