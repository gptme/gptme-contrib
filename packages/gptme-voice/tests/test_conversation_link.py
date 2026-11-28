"""Tests for binding a browser voice call to a gptme-server conversation."""

import asyncio
import json
import os
from dataclasses import dataclass

import gptme_voice.realtime.tool_bridge as tool_bridge_module
import httpx
import pytest
from gptme_voice.realtime import conversation_link
from gptme_voice.realtime.conversation_link import (
    CONTEXT_MAX_CHARS_PER_MESSAGE,
    LOOKUP_MAX_TOOL_CALLS,
    ConversationLookup,
    LiveTranscriptSink,
    fetch_conversation_context,
    format_conversation_context,
)
from gptme_voice.realtime.tool_bridge import GptmeToolBridge, ToolResult


@dataclass
class Turn:
    role: str
    text: str


def test_format_context_keeps_recent_user_and_assistant_text() -> None:
    long = "x" * (CONTEXT_MAX_CHARS_PER_MESSAGE + 50)
    text = format_conversation_context(
        [
            {"role": "system", "content": "huge system prompt"},
            {"role": "user", "content": "My favourite colour is teal."},
            {"role": "assistant", "content": long},
            {"role": "user", "content": "   "},
        ]
    )
    assert text is not None
    assert "huge system prompt" not in text
    assert "User: My favourite colour is teal." in text
    assert "[...]" in text
    assert format_conversation_context([{"role": "system", "content": "x"}]) is None


def test_fetch_context_reads_log_and_workspace() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer key"
        if request.url.path.endswith("/config"):
            return httpx.Response(200, json={"chat": {"workspace": "/tmp/ws"}})
        return httpx.Response(
            200, json={"log": [{"role": "user", "content": "hi there"}]}
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await fetch_conversation_context(
                "http://server", "key", "conv/1", client=c
            )

    ctx = asyncio.run(run())
    assert ctx.workspace == "/tmp/ws"
    assert os.path.isabs(ctx.workspace)
    assert ctx.instructions and "User: hi there" in ctx.instructions


def test_fetch_context_never_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await fetch_conversation_context("http://s", "k", "c", client=c)

    ctx = asyncio.run(run())
    assert ctx.instructions is None and ctx.workspace is None


@pytest.mark.parametrize(
    "log",
    [{"role": "user"}, ["a bare string", {"role": "user", "content": "hi"}], "text"],
)
def test_fetch_context_tolerates_a_malformed_log(log: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/config"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"log": log})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await fetch_conversation_context("http://s", "k", "c", client=c)

    ctx = asyncio.run(run())  # never raises
    if isinstance(log, list):
        assert ctx.instructions and "User: hi" in ctx.instructions
    else:
        assert ctx.instructions is None


def test_fetch_context_marks_the_workspace_unknown_when_config_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/config"):
            return httpx.Response(500)
        return httpx.Response(200, json={"log": []})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await fetch_conversation_context("http://s", "k", "c", client=c)

    ctx = asyncio.run(run())
    assert ctx.workspace is None and ctx.workspace_known is False


def test_fetch_context_treats_a_relative_workspace_as_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/config"):
            return httpx.Response(200, json={"chat": {"workspace": "../other-agent"}})
        return httpx.Response(200, json={"log": []})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await fetch_conversation_context("http://s", "k", "c", client=c)

    ctx = asyncio.run(run())
    assert ctx.workspace is None and ctx.workspace_known is False


def test_fetch_context_tolerates_malformed_config() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/config"):
            return httpx.Response(200, json={"chat": ["not", "a", "dict"]})
        return httpx.Response(200, json={"log": [{"role": "user", "content": "hi"}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await fetch_conversation_context("http://s", "k", "c", client=c)

    ctx = asyncio.run(run())
    assert ctx.workspace is None
    assert ctx.instructions and "User: hi" in ctx.instructions


@pytest.mark.parametrize(
    ("status", "expected"),
    [(500, "retry"), (503, "retry"), (429, "retry"), (408, "retry"), (403, "error")],
)
def test_sink_retries_transient_errors_and_gives_up_on_permanent(
    status: int, expected: str
) -> None:
    responses = [status, 200]
    posts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(responses.pop(0), json={})

    transcript = [Turn("user", "hello")]

    async def run() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            sink = LiveTranscriptSink(
                server_url="http://server",
                api_key="key",
                conversation_id="conv-1",
                call_sid="webui-abc",
                transcript=transcript,
                client=c,
            )
            return [await sink.flush(), await sink.flush()]

    first, second = asyncio.run(run())
    assert first == expected
    assert second == "ok"
    if expected == "retry":
        # A transient failure leaves the turn pending; the next flush posts it.
        assert len(posts) == 2
    else:
        # A permanent rejection drops the turn instead of re-sending it.
        assert len(posts) == 1


def test_sink_posts_each_turn_once_with_stable_ids_and_retries_409() -> None:
    posts: list[dict] = []
    responses = [409, 200, 200]

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(responses.pop(0), json={})

    transcript: list[Turn] = []

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            sink = LiveTranscriptSink(
                server_url="http://server",
                api_key="key",
                conversation_id="conv-1",
                call_sid="webui-abc",
                transcript=transcript,
                call_metadata={"source": "webui"},
                client=c,
                settle_seconds=0.01,
                retry_seconds=0.01,
            )
            transcript.append(Turn("user", "hello"))
            assert await sink.flush() == "retry"  # generation in progress
            assert await sink.flush() == "ok"
            transcript.append(Turn("assistant", "hi!"))
            await sink.close()

    asyncio.run(run())
    assert [p["turns"] for p in posts] == [
        [{"role": "user", "text": "hello", "turn_id": "webui-abc:0"}],
        [{"role": "user", "text": "hello", "turn_id": "webui-abc:0"}],
        [{"role": "assistant", "text": "hi!", "turn_id": "webui-abc:1"}],
    ]
    assert posts[0]["call_metadata"] == {"source": "webui", "call_sid": "webui-abc"}


def test_immediate_flush_holds_an_unsettled_speech_turn_and_keeps_order() -> None:
    """A typed turn flushes at once, but an earlier speech partial still in its
    settle window is not posted early; both follow once it settles, in order."""
    posts: list[list[dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content)["turns"])
        return httpx.Response(200, json={})

    transcript: list[Turn] = []

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            sink = LiveTranscriptSink(
                server_url="http://server",
                api_key="key",
                conversation_id="conv-1",
                call_sid="webui-abc",
                transcript=transcript,
                client=c,
                settle_seconds=0.2,
            )
            transcript.append(Turn("user", "Hello, use"))  # speech partial
            sink.notify()
            transcript.append(Turn("user", "typed: stop"))
            sink.notify(immediate=True)
            await asyncio.sleep(0.05)
            assert posts == []  # held behind the unsettled partial
            transcript[0] = Turn("user", "Hello, use the other file")
            sink.notify()
            await asyncio.sleep(0.5)
            await sink.close()

    asyncio.run(run())
    assert posts == [
        [
            {
                "role": "user",
                "text": "Hello, use the other file",
                "turn_id": "webui-abc:0",
            },
            {"role": "user", "text": "typed: stop", "turn_id": "webui-abc:1"},
        ]
    ]


def test_close_lets_an_in_flight_post_finish() -> None:
    """Closing while the timer's POST is in flight must not cancel it: the
    turn is posted exactly once, not lost or sent again by the final flush."""
    posts: list[list[dict]] = []
    sent: list[int] = []
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.append(1)
        started.set()
        await asyncio.sleep(0.2)
        posts.append(json.loads(request.content)["turns"])
        return httpx.Response(200, json={})

    transcript: list[Turn] = []

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            sink = LiveTranscriptSink(
                server_url="http://server",
                api_key="key",
                conversation_id="conv-1",
                call_sid="webui-abc",
                transcript=transcript,
                client=c,
            )
            transcript.append(Turn("user", "typed: hi"))
            sink.notify(immediate=True)
            await asyncio.wait_for(started.wait(), 1)
            await sink.close()

    asyncio.run(run())
    assert posts == [[{"role": "user", "text": "typed: hi", "turn_id": "webui-abc:0"}]]
    assert len(sent) == 1  # not cancelled and sent again


def test_settle_window_holds_even_when_configured_tiny() -> None:
    sink = LiveTranscriptSink(
        server_url="http://server",
        api_key="key",
        conversation_id="conv-1",
        call_sid="webui-abc",
        transcript=[Turn("user", "partial")],
        settle_seconds=0.01,
    )
    sink._observe()
    # Just observed: not settled, even though settle - 0.05 would be negative.
    assert not sink._settled(0, "partial", sink._seen[0][1])


def test_notify_during_a_post_does_not_cancel_it() -> None:
    """A new turn while a flush is mid-POST waits for that POST, then posts."""
    posts: list[list[str]] = []
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        posts.append([t["text"] for t in json.loads(request.content)["turns"]])
        if len(posts) == 1:
            await gate.wait()
        return httpx.Response(200, json={})

    transcript = [Turn("user", "first")]

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            sink = LiveTranscriptSink(
                server_url="http://server",
                api_key="key",
                conversation_id="conv-1",
                call_sid="webui-abc",
                transcript=transcript,
                client=c,
                settle_seconds=0.01,
            )
            sink.notify(immediate=True)
            await asyncio.sleep(0.05)  # the first POST is in flight
            transcript.append(Turn("user", "second"))
            sink.notify(immediate=True)
            await asyncio.sleep(0.05)
            gate.set()
            await asyncio.sleep(0.1)
            await sink.close()

    asyncio.run(run())
    assert posts == [["first"], ["second"]]


def test_a_notify_during_a_busy_post_keeps_the_retry_backoff() -> None:
    """409 during a POST that a notify arrived for: wait retry_seconds, don't
    fire the next POST right away."""
    times: list[float] = []
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        times.append(asyncio.get_running_loop().time())
        if len(times) == 1:
            await gate.wait()
            return httpx.Response(409, json={})
        return httpx.Response(200, json={})

    transcript = [Turn("user", "first")]

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            sink = LiveTranscriptSink(
                server_url="http://server",
                api_key="key",
                conversation_id="conv-1",
                call_sid="webui-abc",
                transcript=transcript,
                client=c,
                settle_seconds=0.01,
                retry_seconds=0.3,
            )
            sink.notify(immediate=True)
            await asyncio.sleep(0.05)
            transcript.append(Turn("user", "second"))
            sink.notify(immediate=True)  # arrives mid-POST
            gate.set()
            await asyncio.sleep(0.6)
            await sink.close()

    asyncio.run(run())
    assert len(times) >= 2 and times[1] - times[0] >= 0.25


def test_sink_reposts_a_turn_the_provider_revised() -> None:
    posts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(200, json={})

    transcript: list[Turn] = [Turn("user", "Hello")]

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            sink = LiveTranscriptSink(
                server_url="http://server",
                api_key="key",
                conversation_id="conv-1",
                call_sid="webui-abc",
                transcript=transcript,
                client=c,
            )
            assert await sink.flush() == "ok"
            assert await sink.flush() == "ok"  # unchanged: nothing posted
            transcript[0] = Turn("user", "Hello, use the other file")
            await sink.close()

    asyncio.run(run())
    assert [p["turns"] for p in posts] == [
        [{"role": "user", "text": "Hello", "turn_id": "webui-abc:0"}],
        [
            {
                "role": "user",
                "text": "Hello, use the other file",
                "turn_id": "webui-abc:0",
            }
        ],
    ]


# --- lookups as conversation steps -------------------------------------------


def _sse(*events: dict) -> bytes:
    return b"".join(f"data: {json.dumps(e)}\n\n".encode() for e in events)


# Events after this marker are sent once a step starts (by default, all
# events after the first).
STEP = {"type": "_step"}


class _LookupServer:
    """A fake gptme-server: an SSE stream plus append/step/interrupt endpoints.

    Like the real server, appending a message sends its ``message_added``,
    and a step's events follow the step request.
    """

    def __init__(self, events: list[dict], *, hang: bool = False) -> None:
        if STEP in events:
            at = events.index(STEP)
            self.before, self.after = events[:at], events[at + 1 :]
        else:
            self.before, self.after = events[:1], events[1:]
        self.hang = hang
        self.calls: list[tuple[str, dict]] = []
        self.step_statuses: list[int] = []
        self.queue: asyncio.Queue[dict | None] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/events"):

            async def body():
                self.queue = asyncio.Queue()
                yield _sse(*self.before)
                while (event := await self.queue.get()) is not None:
                    yield _sse(event)
                if self.hang:
                    await asyncio.sleep(3600)

            return httpx.Response(200, content=body())
        payload = json.loads(request.content)
        kind = path.rsplit("/", 1)[-1]
        self.calls.append((kind, payload))
        status = 200
        if kind == "step" and self.step_statuses:
            status = self.step_statuses.pop(0)
        if self.queue is not None and status == 200:
            if kind == "conv-1":
                self.queue.put_nowait(_msg("user", payload["content"]))
            elif kind == "step":
                for event in self.after:
                    self.queue.put_nowait(event)
                self.queue.put_nowait(None)
        return httpx.Response(status, json={})

    def lookup(self, client: httpx.AsyncClient, **kwargs) -> ConversationLookup:
        return ConversationLookup(
            server_url="http://server",
            api_key="key",
            conversation_id="conv-1",
            client=client,
            **kwargs,
        )


def _run_lookup(server: _LookupServer, task: str = "star count?", **kwargs):
    async def run():
        transport = httpx.MockTransport(server.handler)
        async with httpx.AsyncClient(transport=transport) as c:
            return await server.lookup(c, **kwargs)(task, mode="fast")

    return asyncio.run(run())


def _msg(role: str, content: str) -> dict:
    return {"type": "message_added", "message": {"role": role, "content": content}}


def test_lookup_runs_as_a_step_and_returns_the_last_assistant_reply() -> None:
    server = _LookupServer(
        [
            {"type": "connected", "session_id": "s1", "generating": False},
            _msg("assistant", "a spoken voice turn, not the answer"),
            {"type": "generation_started"},
            _msg("assistant", "Let me check.\n```shell\ngh api ...\n```"),
            {"type": "tool_pending", "tool_id": "t1", "auto_confirm": True},
            # The first generation ends; the tool runs; a continuation starts.
            {"type": "generation_complete"},
            {"type": "step_complete"},
            {"type": "tool_executing", "tool_id": "t1"},
            _msg("system", "Ran command: 4321"),
            {"type": "tool_complete", "tool_id": "t1"},
            {"type": "generation_started"},
            _msg("assistant", "<think>\nreport it\n</think>\n\ngptme has 4321 stars."),
            {"type": "generation_complete"},
            {"type": "step_complete"},
        ]
    )
    order: list[str] = []

    async def before() -> None:
        order.append(f"flush after {len(server.calls)} calls")

    result = _run_lookup(server, before_lookup=before)

    assert result.success and result.output == "gptme has 4321 stars."
    assert order == ["flush after 0 calls"]
    (append_kind, append), (step_kind, step) = server.calls
    assert append_kind == "conv-1" and append["role"] == "user"
    assert append["content"].startswith("[Voice call lookup] star count?")
    assert step_kind == "step"
    assert step == {"session_id": "s1", "auto_confirm": LOOKUP_MAX_TOOL_CALLS}


def test_lookup_waits_for_a_running_reply_before_adding_to_the_chat() -> None:
    server = _LookupServer(
        [
            {"type": "connected", "session_id": "s1", "generating": True},
            {"type": "step_complete"},  # the user's own text reply finished
            STEP,
            {"type": "generation_started"},
            _msg("assistant", "done"),
            {"type": "step_complete"},
        ]
    )
    result = _run_lookup(server)
    assert result.success and result.output == "done"
    assert [k for k, _ in server.calls] == ["conv-1", "step"]


def test_lookup_retries_a_busy_step(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conversation_link, "LOOKUP_BUSY_RETRY_SECONDS", 0.01)
    server = _LookupServer(
        [
            {"type": "connected", "session_id": "s1"},
            {"type": "generation_started"},
            _msg("assistant", "ok"),
            {"type": "step_complete"},
        ]
    )
    server.step_statuses = [409, 409, 200]
    result = _run_lookup(server)
    assert result.success
    assert [k for k, _ in server.calls] == ["conv-1", "step", "step", "step"]


@pytest.mark.parametrize(
    ("event", "error"),
    [
        ({"type": "error", "error": "model refused"}, "model refused"),
        ({"type": "tool_pending", "auto_confirm": False}, "confirmation click"),
    ],
)
def test_lookup_failure_interrupts_the_step(event: dict, error: str) -> None:
    server = _LookupServer(
        [
            {"type": "connected", "session_id": "s1"},
            {"type": "generation_started"},
            event,
        ]
    )
    result = _run_lookup(server)
    assert not result.success and error in (result.error or "")
    assert server.calls[-1] == ("interrupt", {"session_id": "s1"})


def test_lookup_timeout_interrupts_the_step() -> None:
    server = _LookupServer(
        [{"type": "connected", "session_id": "s1"}, {"type": "generation_started"}],
        hang=True,
    )
    result = _run_lookup(server, timeouts={"fast": 0.2})
    assert not result.success and "did not finish" in (result.error or "")
    assert server.calls[-1] == ("interrupt", {"session_id": "s1"})


def test_lookup_cancel_interrupts_the_step() -> None:
    server = _LookupServer(
        [{"type": "connected", "session_id": "s1"}, {"type": "generation_started"}],
        hang=True,
    )

    async def run() -> None:
        transport = httpx.MockTransport(server.handler)
        async with httpx.AsyncClient(transport=transport) as c:
            task = asyncio.create_task(server.lookup(c)("q", mode="fast"))
            for _ in range(100):
                if any(k == "step" for k, _ in server.calls):
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    assert server.calls[-1] == ("interrupt", {"session_id": "s1"})


def test_lookup_without_an_answer_fails() -> None:
    server = _LookupServer(
        [
            {"type": "connected", "session_id": "s1"},
            _msg("assistant", "a voice turn posted before the step started"),
            {"type": "generation_started"},
            {"type": "step_complete"},
        ]
    )
    result = _run_lookup(server)
    assert not result.success and "no answer" in (result.error or "")


def test_lookup_ignores_text_from_a_generation_before_the_last() -> None:
    server = _LookupServer(
        [
            {"type": "connected", "session_id": "s1"},
            {"type": "generation_started"},
            _msg("assistant", "Let me check."),
            {"type": "tool_pending", "tool_id": "t1", "auto_confirm": True},
            {"type": "step_complete"},
            {"type": "generation_started"},  # the continuation says nothing
            {"type": "step_complete"},
        ]
    )
    result = _run_lookup(server)
    assert not result.success and "no answer" in (result.error or "")


def test_lookup_timeout_reports_a_timeout_returncode() -> None:
    server = _LookupServer(
        [{"type": "connected", "session_id": "s1"}, {"type": "generation_started"}],
        hang=True,
    )
    completed: list[int] = []

    async def run():
        transport = httpx.MockTransport(server.handler)
        async with httpx.AsyncClient(transport=transport) as c:
            lookup = server.lookup(c, timeouts={"fast": 0.2})
            return await lookup(
                "q", mode="fast", on_completed=lambda rc, _t: completed.append(rc)
            )

    result = asyncio.run(run())
    assert not result.success and completed == [124]


def test_lookup_fails_when_the_request_cannot_be_added_to_the_chat() -> None:
    server = _LookupServer([{"type": "connected", "session_id": "s1"}])
    original = server.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/conv-1"):
            return httpx.Response(500, json={})
        return original(request)

    server.handler = handler  # type: ignore[method-assign]
    result = _run_lookup(server)
    assert not result.success
    assert all(kind != "step" for kind, _ in server.calls)


def test_lookup_interrupts_a_step_whose_post_got_no_response() -> None:
    server = _LookupServer([{"type": "connected", "session_id": "s1"}])
    original = server.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/step"):
            raise httpx.ReadTimeout("no response", request=request)
        return original(request)

    server.handler = handler  # type: ignore[method-assign]
    result = _run_lookup(server)
    assert not result.success
    assert server.calls[-1] == ("interrupt", {"session_id": "s1"})


def test_lookup_busy_chat_does_not_interrupt_the_other_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conversation_link, "LOOKUP_BUSY_RETRY_SECONDS", 0.05)
    server = _LookupServer([{"type": "connected", "session_id": "s1"}], hang=True)
    server.step_statuses = [409] * 100
    completed: list[int] = []

    async def run():
        transport = httpx.MockTransport(server.handler)
        async with httpx.AsyncClient(transport=transport) as c:
            lookup = server.lookup(c, timeouts={"fast": 0.2})
            return await lookup(
                "q", mode="fast", on_completed=lambda rc, _t: completed.append(rc)
            )

    result = asyncio.run(run())
    # Busy is not a timeout: no timeout cue, and nothing to interrupt.
    assert not result.success and "busy" in (result.error or "")
    assert completed == []
    assert all(kind != "interrupt" for kind, _ in server.calls)


def test_interrupted_lookup_is_not_an_answer() -> None:
    server = _LookupServer(
        [
            {"type": "connected", "session_id": "s1"},
            {"type": "generation_started"},
            _msg("assistant", "Half an ans"),
            {"type": "interrupted"},
        ]
    )
    result = _run_lookup(server)
    assert not result.success and "interrupted" in (result.error or "")


def test_sse_event_data_may_span_lines() -> None:
    async def lines():
        for line in ['data: {"type":', 'data: "ping"}', "", ": comment", ""]:
            yield line

    async def collect():
        return [e async for e in conversation_link._sse_events(lines())]

    assert asyncio.run(collect()) == [{"type": "ping"}]


def test_lookup_ignores_a_reply_that_is_not_its_own() -> None:
    """Events carry no step id: a reply that ran before our request (or ended
    after it, before our step) must not be taken for the lookup's answer."""
    server = _LookupServer(
        [
            {"type": "connected", "session_id": "s1", "generating": False},
            {"type": "generation_started"},  # another client's reply
            _msg("assistant", "an unrelated auto-reply"),
            STEP,
            {"type": "step_complete"},  # ...which ends after our request
            {"type": "generation_started"},
            _msg("assistant", "the lookup answer"),
            {"type": "step_complete"},
        ]
    )
    result = _run_lookup(server)
    assert result.success and result.output == "the lookup answer"


def test_lookup_waits_out_a_running_reply_that_ends_in_an_error() -> None:
    server = _LookupServer(
        [
            {"type": "connected", "session_id": "s1", "generating": True},
            {"type": "error", "error": "the user's reply failed"},
            STEP,
            {"type": "generation_started"},
            _msg("assistant", "done"),
            {"type": "step_complete"},
        ]
    )
    result = _run_lookup(server, timeouts={"fast": 2})
    assert result.success and result.output == "done"


def test_lookup_budgets_follow_the_subprocess_env_vars() -> None:
    from gptme_voice.realtime import tool_bridge

    assert conversation_link.LOOKUP_TIMEOUT_SECONDS == {
        "fast": float(tool_bridge._FAST_MODE_BUDGET_SECONDS),
        "smart": float(tool_bridge._SMART_MODE_BUDGET_SECONDS),
    }


def test_lookup_retries_posting_the_spoken_turns_while_the_chat_is_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conversation_link, "LOOKUP_BUSY_RETRY_SECONDS", 0.01)
    server = _LookupServer(
        [
            {"type": "connected", "session_id": "s1"},
            {"type": "generation_started"},
            _msg("assistant", "ok"),
            {"type": "step_complete"},
        ]
    )
    flushes: list[str] = []

    async def before() -> str:
        flushes.append(f"after {len(server.calls)} calls")
        return "retry" if len(flushes) < 3 else "ok"

    result = _run_lookup(server, before_lookup=before)
    assert result.success
    assert flushes == ["after 0 calls"] * 3  # all before the request is added


def test_execute_delegates_to_the_lookup_runner() -> None:
    """With a lookup runner (webui calls), no gptme subprocess is spawned."""
    seen: list[tuple] = []

    async def _runner(task, *, mode, on_started, on_progress, on_completed):
        seen.append((task, mode))
        on_started(1.0)
        return ToolResult(success=True, output="42")

    async def _no_spawn(*args, **kwargs):
        raise AssertionError("spawned a subprocess")

    async def _exercise() -> None:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge_module, "_spawn_subagent", _no_spawn)
            bridge = GptmeToolBridge(workspace="/fake", lookup_runner=_runner)
            started: list[float] = []
            result = await bridge._execute(
                "the answer", mode="smart", on_started=started.append
            )
        assert result.output == "42" and started == [1.0]

    asyncio.run(_exercise())
    assert seen == [("the answer", "smart")]
