"""Bind a browser voice call to a gptme-server conversation.

When gptme-server runs gptme-voice as its voice sidecar, it opens ``/voice``
with ``?conversation_id=<id>``. This module lets the call:

- read the conversation (recent messages and workspace) so the realtime
  model knows what the user was doing, and
- append transcript turns to that conversation while the call runs, so they
  show up live in the webui next to typed messages, and
- run lookups as steps of that conversation (:class:`ConversationLookup`), so
  they appear in the chat and use its workspace, model and tools.

Both use the server's HTTP API with ``GPTME_VOICE_GPTME_SERVER_URL`` and
``GPTME_VOICE_GPTME_SERVER_KEY``, which the server sets when it starts the
sidecar.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Set
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from .tool_bridge import (
    _FAST_MODE_BUDGET_SECONDS,
    _SMART_MODE_BUDGET_SECONDS,
    ToolResult,
)

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
    # False when the conversation's config couldn't be read, so its workspace
    # is unknown (not "the server's default"): callers must fail closed.
    workspace_known: bool = True
    # False when the conversation itself couldn't be loaded (404, auth, ...):
    # nothing may be posted to it.
    loaded: bool = True


def _conversation_url(server_url: str, conversation_id: str, suffix: str = "") -> str:
    quoted = urllib.parse.quote(conversation_id, safe="")
    return f"{server_url.rstrip('/')}/api/v2/conversations/{quoted}{suffix}"


def format_conversation_context(messages: Sequence[dict[str, Any]]) -> str | None:
    """Render recent user/assistant messages as instructions for the voice model."""
    lines: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
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

    async def _load(
        c: httpx.AsyncClient,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        conversation = await _get(c, "")
        try:
            config: dict[str, Any] | None = await _get(c, "/config")
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Could not load the config of %s: %s", conversation_id, exc)
            config = None
        return conversation, config

    try:
        if client is None:
            async with httpx.AsyncClient(timeout=5) as own_client:
                conversation, config = await _load(own_client)
        else:
            conversation, config = await _load(client)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Could not load conversation %s: %s", conversation_id, exc)
        return ConversationContext(
            instructions=None, workspace=None, workspace_known=False, loaded=False
        )

    log = conversation.get("log")
    chat_config = config.get("chat") if config is not None else None
    workspace = chat_config.get("workspace") if isinstance(chat_config, dict) else None
    ws_path = (
        os.path.expanduser(workspace)
        if isinstance(workspace, str) and workspace
        else None
    )
    absolute = ws_path is not None and os.path.isabs(ws_path)
    return ConversationContext(
        instructions=format_conversation_context(log if isinstance(log, list) else []),
        workspace=ws_path if absolute else None,
        # A relative path would resolve against this sidecar's cwd, not the
        # server's: treat it as unknown rather than guess.
        workspace_known=config is not None and (absolute or ws_path is None),
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
        # Index -> (text, when it last changed), to tell settled turns apart.
        self._seen: dict[int, tuple[str, float]] = {}
        # Turns that cannot be revised (typed messages).
        self._final: set[int] = set()
        self._timer: asyncio.Task[None] | None = None
        self._flushing = False  # the timer task is inside flush()
        self._again: float | None = None  # a notify arrived during that flush
        self._lock = asyncio.Lock()
        self._closed = False

    def notify(self, *, immediate: bool = False) -> None:
        """Call after the transcript changed; (re)starts the settle timer.

        *immediate* marks the newest turn as final (a typed message, unlike a
        provider speech transcript, is never revised) and flushes without
        waiting. Earlier turns still in their settle window are not posted
        early: the flush stops there and retries once they settle, so the
        chat keeps the call's order.
        """
        if self._closed:
            return
        self._observe()
        if immediate and self._transcript:
            self._final.add(len(self._transcript) - 1)
        self._schedule(0 if immediate else self._settle_seconds)

    def _observe(self) -> None:
        now = time.monotonic()
        for i, turn in enumerate(self._transcript):
            text = str(turn.text)
            seen = self._seen.get(i)
            if seen is None or seen[0] != text:
                self._seen[i] = (text, now)

    def _settled(self, i: int, text: str, now: float) -> bool:
        if i in self._final:
            return True
        seen = self._seen.get(i)
        if seen is None or seen[0] != text:
            return False
        # Small slack (the timer fires settle_seconds after the last change),
        # never more than half the window, so a tiny window still holds.
        slack = min(0.05, self._settle_seconds / 2)
        return now - seen[1] >= self._settle_seconds - slack

    def _schedule(self, delay: float) -> None:
        if self._timer is not None and not self._timer.done():
            if self._flushing:
                # Never cancel a POST in flight; flush again right after it.
                self._again = delay if self._again is None else min(self._again, delay)
                return
            self._timer.cancel()
        self._timer = asyncio.create_task(self._flush_after(delay))

    async def _flush_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self._flushing = True
        try:
            result = await self.flush(force=False)
        finally:
            self._flushing = False
        if self._closed:
            return
        again, self._again = self._again, None
        if result == "retry":
            # Keep the backoff; turns that arrived meanwhile go with the retry.
            self._schedule(self._retry_seconds)
        elif result == "held":
            self._schedule(self._settle_seconds)
        elif again is not None:
            self._schedule(again)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10)
        return self._client

    async def flush(self, *, force: bool = True) -> str:
        """Post unposted turns. Returns "ok", "held", "retry" or "error".

        Without *force*, posting stops before the first turn still in its
        settle window ("held": call again once it settled).
        """
        async with self._lock:
            self._observe()
            now = time.monotonic()
            pending = []
            held = False
            for i, turn in enumerate(self._transcript):
                if not str(turn.text).strip() or self._posted.get(i) == turn.text:
                    continue
                if not force and not self._settled(i, str(turn.text), now):
                    held = True
                    break
                pending.append((i, turn))
            if not pending:
                return "held" if held else "ok"
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
            # Recorded as handled either way: a permanent rejection (auth, a
            # malformed request) would fail again, so those turns are dropped
            # rather than re-sent with every later turn.
            self._posted.update((i, turn.text) for i, turn in pending)
            if resp.status_code >= 400:
                logger.warning(
                    "Live transcript post rejected (%d), dropped %d turn(s): %s",
                    resp.status_code,
                    len(pending),
                    resp.text[:200],
                )
                return "error"
            return "held" if held else "ok"

    async def close(self) -> None:
        """Flush everything (retrying briefly) and release the HTTP client."""
        self._closed = True
        if self._timer is not None and not self._timer.done():
            if not self._flushing:
                self._timer.cancel()
            # A POST in flight runs to completion, so its turns are marked
            # posted (or kept for the final flush) instead of lost.
            await asyncio.gather(self._timer, return_exceptions=True)
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


# Lookups run as steps of the conversation itself (instead of a separate gptme
# subprocess), so they show up in the chat and use its workspace, model and
# tools. The step may run this many tools without a confirmation click.
LOOKUP_MAX_TOOL_CALLS = 25
# The same budgets (and env vars) as the subprocess lookup.
LOOKUP_TIMEOUT_SECONDS = {
    "fast": float(_FAST_MODE_BUDGET_SECONDS),
    "smart": float(_SMART_MODE_BUDGET_SECONDS),
}
LOOKUP_RESULT_MAX_CHARS = 4000
LOOKUP_CONNECT_SECONDS = 10.0
LOOKUP_BUSY_RETRY_SECONDS = 1.0
_STEP_DONE_EVENTS = frozenset({"step_complete", "interrupted"})
# How a reply that is not ours can end (an error ends it too).
_REPLY_DONE_EVENTS = _STEP_DONE_EVENTS | {"error"}
_LOOKUP_PREFIX = "[Voice call lookup] "
_LOOKUP_SUFFIX = (
    "\n\n(Asked by the voice assistant during the call: you are its lookup, "
    "so use your own tools directly. Answer briefly in plain text; the voice "
    "assistant reads your answer aloud.)"
)


# Reasoning blocks some models emit inline; never read aloud.
_THINK_RE = re.compile(r"<think(?:ing)?>.*?(?:</think(?:ing)?>|$)", re.DOTALL)


class _LookupFailed(Exception):
    pass


class _EventPump:
    """Reads an event stream in a background task.

    Waiting on it with a timeout is then safe: a timed-out wait on the stream
    itself would cancel its pending read and so close it.
    """

    def __init__(self, events: AsyncIterator[dict[str, Any]]) -> None:
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._task = asyncio.create_task(self._pump(events))

    async def _pump(self, events: AsyncIterator[dict[str, Any]]) -> None:
        try:
            async for event in events:
                self._queue.put_nowait(event)
        finally:
            self._queue.put_nowait(None)

    def __aiter__(self) -> _EventPump:
        return self

    async def __anext__(self) -> dict[str, Any]:
        event = await self._queue.get()
        if event is None:
            self._queue.put_nowait(None)  # stays closed
            raise StopAsyncIteration
        return event

    async def aclose(self) -> None:
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)


class _ChatBusy(_LookupFailed):
    """Another reply kept the chat busy until the deadline; no step started."""

    def __init__(self) -> None:
        super().__init__("the chat stayed busy with another reply, so no lookup ran")


class ConversationLookup:
    """Run a voice lookup as a step of the call's conversation.

    Appends the request as a user message, takes a step with a tool budget,
    waits on the conversation's event stream for the step to finish, and
    returns the last assistant reply. Cancellation and timeouts interrupt the
    step. Callable with the signature of ``GptmeToolBridge._execute``.
    """

    def __init__(
        self,
        *,
        server_url: str,
        api_key: str,
        conversation_id: str,
        before_lookup: Callable[[], Awaitable[object]] | None = None,
        client: httpx.AsyncClient | None = None,
        timeouts: Mapping[str, float] | None = None,
    ) -> None:
        self._server_url = server_url
        self._conversation_id = conversation_id
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._before_lookup = before_lookup
        self._client = client
        self._timeouts = dict(timeouts or LOOKUP_TIMEOUT_SECONDS)

    def _url(self, suffix: str = "") -> str:
        return _conversation_url(self._server_url, self._conversation_id, suffix)

    async def __call__(
        self,
        task: str,
        mode: str = "fast",
        on_started: Callable[[float], None] | None = None,
        on_progress: Callable[[str, float], None] | None = None,
        on_completed: Callable[[int, float], None] | None = None,
    ) -> ToolResult:
        timeout = self._timeouts.get(mode, max(self._timeouts.values()))
        deadline = time.monotonic() + timeout
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, read=None)
        )
        session_id: str | None = None
        stepping = False
        try:
            async with client.stream(
                "GET", self._url("/events"), headers=self._headers
            ) as resp:
                resp.raise_for_status()
                events = _EventPump(_sse_events(resp.aiter_lines()))
                try:
                    connected = await _next_event_of(
                        events, {"connected"}, time.monotonic() + LOOKUP_CONNECT_SECONDS
                    )
                    session_id = str(connected.get("session_id") or "")
                    if not session_id:
                        raise _LookupFailed("the server sent no session id")
                    if connected.get("generating"):
                        # Let a running text reply finish before adding to the chat.
                        await _next_event_of(events, _REPLY_DONE_EVENTS, deadline)
                    if self._before_lookup is not None:
                        # Post the caller's pending spoken turns first, so the
                        # chat reads question, lookup, answer; "retry" means
                        # the chat is busy, so try again until it is posted.
                        while await self._before_lookup() == "retry":
                            if time.monotonic() + LOOKUP_BUSY_RETRY_SECONDS > deadline:
                                raise _ChatBusy
                            await asyncio.sleep(LOOKUP_BUSY_RETRY_SECONDS)
                    request = _LOOKUP_PREFIX + task + _LOOKUP_SUFFIX
                    appended = await self._post(
                        "", {"role": "user", "content": request}
                    )
                    appended.raise_for_status()
                    # Events carry no session or step id. The stream is
                    # ordered, so nothing before our own request is ours.
                    await _until_own_request(events, request, deadline)
                    # A step POST that got no response may still have started
                    # the step: interrupt on any error from here, unless the
                    # server answered (busy, or an error status).
                    stepping = True
                    try:
                        await self._start_step(session_id, events, deadline)
                    except (_ChatBusy, httpx.HTTPStatusError):
                        stepping = False
                        raise
                    if on_started:
                        on_started(time.monotonic())
                    answer = await self._await_answer(events, deadline, on_progress)
                    stepping = False
                finally:
                    await events.aclose()
        except asyncio.CancelledError:
            if stepping and session_id:
                await asyncio.shield(self._interrupt(session_id))
            raise
        except asyncio.TimeoutError:
            if stepping and session_id:
                await self._interrupt(session_id)
            if on_completed:
                on_completed(124, time.monotonic())  # as a timed-out subprocess
            return ToolResult(
                success=False,
                output="",
                error=f"the lookup in the chat did not finish within {timeout:.0f} s",
            )
        except (httpx.HTTPError, _LookupFailed, ValueError) as exc:
            if stepping and session_id:
                await self._interrupt(session_id)
            logger.warning("Conversation lookup failed: %s", exc)
            return ToolResult(success=False, output="", error=str(exc) or "failed")
        finally:
            if self._client is None:
                await client.aclose()
        if on_completed:
            on_completed(0, time.monotonic())
        if not answer:
            return ToolResult(
                success=False, output="", error="the chat agent gave no answer"
            )
        if len(answer) > LOOKUP_RESULT_MAX_CHARS:
            answer = answer[:LOOKUP_RESULT_MAX_CHARS].rstrip() + " [...]"
        return ToolResult(success=True, output=answer)

    async def _post(self, suffix: str, payload: dict[str, Any]) -> httpx.Response:
        client = self._client or httpx.AsyncClient(timeout=10)
        try:
            return await client.post(
                self._url(suffix), json=payload, headers=self._headers
            )
        finally:
            if self._client is None:
                await client.aclose()

    async def _start_step(
        self, session_id: str, events: _EventPump, deadline: float
    ) -> None:
        payload = {"session_id": session_id, "auto_confirm": LOOKUP_MAX_TOOL_CALLS}
        while True:
            resp = await self._post("/step", payload)
            if resp.status_code != 409:
                resp.raise_for_status()
                return
            # Another reply (or a command) is running. Read up to its end, so
            # its events are not taken for ours, then try again; a command
            # sends no step events, so retry after a while regardless.
            if time.monotonic() + LOOKUP_BUSY_RETRY_SECONDS > deadline:
                raise _ChatBusy
            try:
                await _next_event_of(
                    events,
                    _REPLY_DONE_EVENTS,
                    time.monotonic() + LOOKUP_BUSY_RETRY_SECONDS,
                )
            except asyncio.TimeoutError:
                pass

    async def _await_answer(
        self,
        events: AsyncIterator[dict[str, Any]],
        deadline: float,
        on_progress: Callable[[str, float], None] | None,
    ) -> str:
        answer = ""
        # Only messages the step generates count; anything added before it
        # started (our request, a voice turn) is not the answer.
        generating = False
        # step_complete ends each generation. When a generation called tools,
        # the server runs them and starts a continuation, so the lookup is
        # done only after a generation that called none.
        ran_tools = False
        while True:
            event = await _next_event_of(
                events,
                {
                    "generation_started",
                    "message_added",
                    "error",
                    "tool_pending",
                    *_STEP_DONE_EVENTS,
                },
                deadline,
            )
            kind = event.get("type")
            if kind == "generation_started":
                # Only the final generation's text answers: an earlier one's
                # "Let me check" is not the result.
                generating = True
                ran_tools = False
                answer = ""
            elif kind == "message_added":
                message = event.get("message")
                if not generating or not isinstance(message, dict):
                    continue
                content = _THINK_RE.sub("", str(message.get("content") or "")).strip()
                if message.get("role") == "assistant" and content:
                    answer = content
                if content and on_progress:
                    on_progress(content.splitlines()[0][:200], time.monotonic())
            elif kind == "error":
                raise _LookupFailed(str(event.get("error") or "the step failed"))
            elif not generating:
                # A reply that was not ours (it started before our step):
                # keep waiting for our generation.
                continue
            elif kind == "tool_pending":
                # Every tool is announced; one left for a click means the
                # budget is used up, or the tool always needs confirmation.
                if event.get("auto_confirm") is False:
                    raise _LookupFailed(
                        "the lookup reached a tool call that needs a confirmation "
                        f"click in the chat (or used all {LOOKUP_MAX_TOOL_CALLS} "
                        "tool calls it may run unconfirmed)"
                    )
                ran_tools = True
            elif kind == "step_complete" and ran_tools:
                continue
            elif kind == "interrupted":
                # Superseded (e.g. the user typed), not finished: partial
                # text is not the answer.
                raise _LookupFailed("the lookup step was interrupted")
            else:
                return answer

    async def _interrupt(self, session_id: str) -> None:
        try:
            await self._post("/interrupt", {"session_id": session_id})
        except httpx.HTTPError as exc:
            logger.warning("Could not interrupt the lookup step: %s", exc)


async def _sse_events(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    """JSON events from an SSE stream; an event's data may span several lines."""
    data: list[str] = []
    async for line in lines:
        if line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
            continue
        if line.strip() or not data:  # event:/id:/comment lines, or no data
            continue
        try:
            event = json.loads("\n".join(data))
        except json.JSONDecodeError:
            continue
        finally:
            data.clear()
        if isinstance(event, dict):
            yield event


async def _until_own_request(
    events: AsyncIterator[dict[str, Any]], request: str, deadline: float
) -> None:
    """Read up to the ``message_added`` of the request we appended."""
    while True:
        event = await _next_event_of(events, {"message_added"}, deadline)
        message = event.get("message")
        if (
            isinstance(message, dict)
            and message.get("role") == "user"
            and str(message.get("content") or "").strip() == request.strip()
        ):
            return


async def _next_event_of(
    events: AsyncIterator[dict[str, Any]], kinds: Set[str], deadline: float
) -> dict[str, Any]:
    """The next event whose type is in *kinds*; TimeoutError past *deadline*."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        try:
            event = await asyncio.wait_for(events.__anext__(), remaining)
        except StopAsyncIteration:
            raise _LookupFailed("the event stream closed") from None
        if event.get("type") in kinds:
            return event
