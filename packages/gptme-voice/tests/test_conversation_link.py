"""Tests for binding a browser voice call to a gptme-server conversation."""

import asyncio
import json
import os
from dataclasses import dataclass

import httpx
import pytest
from gptme_voice.realtime.conversation_link import (
    CONTEXT_MAX_CHARS_PER_MESSAGE,
    LiveTranscriptSink,
    fetch_conversation_context,
    format_conversation_context,
)


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
