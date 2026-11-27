"""Bind a browser voice call to a gptme-server conversation.

When gptme-server runs gptme-voice as its voice sidecar, it opens ``/voice``
with ``?conversation_id=<id>``. This module lets the call:

- read the conversation (recent messages and workspace) so the realtime
  model knows what the user was doing, and
- append transcript turns to that conversation while the call runs, so they
  show up live in the webui next to typed messages.

Both use the server's HTTP API with ``GPTME_VOICE_GPTME_SERVER_URL`` and
``GPTME_VOICE_GPTME_SERVER_KEY``, which the server sets when it starts the
sidecar.
"""

from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

# Recent history handed to the realtime model at session start.
CONTEXT_MAX_MESSAGES = 20
CONTEXT_MAX_CHARS_PER_MESSAGE = 600
CONTEXT_MAX_CHARS = 6000

# A turn is posted once it has gone this long without changing. Some providers
# (xAI) re-send a completed transcript with more text shortly after the first.
SETTLE_SECONDS = 1.2
RETRY_SECONDS = 2.0
_RETRY_STATUSES = frozenset({408, 409, 429})
FINAL_FLUSH_ATTEMPTS = 5


@dataclass
class ConversationContext:
    instructions: str | None
    workspace: str | None


def _conversation_url(server_url: str, conversation_id: str, suffix: str = "") -> str:
    quoted = urllib.parse.quote(conversation_id, safe="")
    return f"{server_url.rstrip('/')}/api/v2/conversations/{quoted}{suffix}"


def format_conversation_context(messages: Sequence[dict[str, Any]]) -> str | None:
    """Render recent user/assistant messages as instructions for the voice model."""
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        if msg.get("hide"):
            continue
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        if len(content) > CONTEXT_MAX_CHARS_PER_MESSAGE:
            content = content[:CONTEXT_MAX_CHARS_PER_MESSAGE].rstrip() + " [...]"
        lines.append(f"{role.title()}: {content}")
    lines = lines[-CONTEXT_MAX_MESSAGES:]
    if not lines:
        return None

    # Keep the newest lines within the character budget.
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        if total + len(line) > CONTEXT_MAX_CHARS:
            break
        kept.append(line)
        total += len(line) + 1
    kept.reverse()
    return (
        "CONVERSATION CONTEXT: This voice call continues an existing gptme chat. "
        "Your spoken turns are added to that chat. The most recent messages "
        "(the text chat is the source of truth):\n" + "\n".join(kept)
    )


async def fetch_conversation_context(
    server_url: str,
    api_key: str,
    conversation_id: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> ConversationContext:
    """Read recent history and the workspace of *conversation_id*.

    Never raises: a call without context is better than no call.
    """
    headers = {"Authorization": f"Bearer {api_key}"}

    async def _get(c: httpx.AsyncClient, suffix: str) -> dict[str, Any]:
        resp = await c.get(
            _conversation_url(server_url, conversation_id, suffix), headers=headers
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def _load(c: httpx.AsyncClient) -> tuple[dict[str, Any], dict[str, Any]]:
        conversation = await _get(c, "")
        try:
            config = await _get(c, "/config")
        except (httpx.HTTPError, ValueError):
            config = {}
        return conversation, config

    try:
        if client is None:
            async with httpx.AsyncClient(timeout=5) as own_client:
                conversation, config = await _load(own_client)
        else:
            conversation, config = await _load(client)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Could not load conversation %s: %s", conversation_id, exc)
        return ConversationContext(instructions=None, workspace=None)

    chat_config = config.get("chat")
    workspace = chat_config.get("workspace") if isinstance(chat_config, dict) else None
    return ConversationContext(
        instructions=format_conversation_context(conversation.get("log") or []),
        workspace=os.path.expanduser(workspace)
        if isinstance(workspace, str) and workspace
        else None,
    )


class LiveTranscriptSink:
    """Post transcript turns to the conversation shortly after each one settles.

    The transcript list is owned by the call handler; it only ever appends a
    turn or replaces the last one (provider partials), so a turn's index is a
    stable id within the call.
    """

    def __init__(
        self,
        *,
        server_url: str,
        api_key: str,
        conversation_id: str,
        call_sid: str,
        transcript: list[Any],
        call_metadata: dict[str, Any] | None = None,
        client: httpx.AsyncClient | None = None,
        settle_seconds: float = SETTLE_SECONDS,
        retry_seconds: float = RETRY_SECONDS,
    ) -> None:
        self._url = _conversation_url(server_url, conversation_id, "/transcript")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._call_sid = call_sid
        self._transcript = transcript
        self._call_metadata = {
            **(call_metadata or {}),
            "call_sid": call_sid,
        }
        self._client = client
        self._owns_client = client is None
        self._settle_seconds = settle_seconds
        self._retry_seconds = retry_seconds
        # Index -> text last posted. A provider can revise a turn after it was
        # posted (e.g. a longer final transcript); revised turns are posted again
        # and the server updates them in place (same turn_id).
        self._posted: dict[int, str] = {}
        self._timer: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    def notify(self, *, immediate: bool = False) -> None:
        """Call after the transcript changed; (re)starts the settle timer.

        *immediate* skips the settle delay, for turns that cannot be revised
        (typed messages, unlike provider speech transcripts).
        """
        if self._closed:
            return
        self._schedule(0 if immediate else self._settle_seconds)

    def _schedule(self, delay: float) -> None:
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        self._timer = asyncio.create_task(self._flush_after(delay))

    async def _flush_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if await self.flush() == "retry" and not self._closed:
            self._schedule(self._retry_seconds)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10)
        return self._client

    async def flush(self) -> str:
        """Post unposted turns. Returns "ok", "retry" or "error"."""
        async with self._lock:
            pending = [
                (i, turn)
                for i, turn in enumerate(self._transcript)
                if str(turn.text).strip() and self._posted.get(i) != turn.text
            ]
            if not pending:
                return "ok"
            payload = {
                "turns": [
                    {
                        "role": turn.role,
                        "text": turn.text,
                        "turn_id": f"{self._call_sid}:{i}",
                    }
                    for i, turn in pending
                ],
                "call_metadata": self._call_metadata,
            }
            try:
                resp = await self._get_client().post(
                    self._url, json=payload, headers=self._headers
                )
            except httpx.HTTPError as exc:
                logger.warning("Live transcript post failed: %s", exc)
                return "retry"
            if resp.status_code in _RETRY_STATUSES or resp.status_code >= 500:
                # 409: the text agent is generating and the server asks us to
                # retry. 408/429/5xx: transient. Unposted turns stay pending.
                if resp.status_code != 409:
                    logger.warning(
                        "Live transcript post failed (%d), will retry",
                        resp.status_code,
                    )
                return "retry"
            if resp.status_code >= 400:
                # Auth or a malformed request: retrying cannot help.
                logger.warning(
                    "Live transcript post rejected (%d): %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return "error"
            self._posted.update((i, turn.text) for i, turn in pending)
            return "ok"

    async def close(self) -> None:
        """Flush everything (retrying briefly) and release the HTTP client."""
        self._closed = True
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        try:
            for attempt in range(FINAL_FLUSH_ATTEMPTS):
                if await self.flush() != "retry":
                    break
                if attempt < FINAL_FLUSH_ATTEMPTS - 1:
                    await asyncio.sleep(self._retry_seconds)
            else:
                logger.warning(
                    "Gave up posting the last transcript turns for call %s",
                    self._call_sid,
                )
        finally:
            if self._owns_client and self._client is not None:
                await self._client.aclose()
