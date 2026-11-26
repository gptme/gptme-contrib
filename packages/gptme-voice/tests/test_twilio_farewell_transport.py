"""Exercise farewell ordering through both provider and Twilio event handlers."""

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from gptme_voice.realtime.openai_client import OpenAIRealtimeClient
from gptme_voice.realtime.server import SessionBootstrap, VoiceServer
from gptme_voice.realtime.twilio_integration import sign_stream_params


class _TwilioSocket:
    query_params = {}

    def __init__(self, timeline):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.timeline = timeline
        self.messages = []
        self.closed = False

    async def accept(self):
        pass

    async def iter_text(self):
        while True:
            item = await self.incoming.get()
            if item is None:
                return
            message, processed = item
            yield json.dumps(message)
            processed.set()

    async def receive_event(self, message):
        processed = asyncio.Event()
        await self.incoming.put((message, processed))
        await asyncio.wait_for(processed.wait(), 1)

    async def send_text(self, text):
        message = json.loads(text)
        self.messages.append(message)
        self.timeline.append(("send", message["event"]))
        await self.outgoing.put(message)

    async def next_event(self, event):
        while True:
            message = await asyncio.wait_for(self.outgoing.get(), 1)
            if message["event"] == event:
                return message

    async def close(self, code=1000):
        self.closed = True
        self.timeline.append(("close", code))
        await self.incoming.put(None)


class _OfflineClient(OpenAIRealtimeClient):
    """Keep real event dispatch and tool result handling, replace only I/O."""

    def __init__(self, **kwargs):
        super().__init__(api_key="test-provider-key", **kwargs)
        self.ready = asyncio.Event()
        self.sent_events = []
        self.disconnected = False

    async def connect(self):
        self.ready.set()

    async def activate_session(self):
        self.ready.set()

    async def disconnect(self, **kwargs):
        self.disconnected = True

    async def _send_event(self, event_type, data):
        self.sent_events.append((event_type, data))


@asynccontextmanager
async def _call(monkeypatch, tmp_path, *, prewarmed=False):
    timeline = []
    credentials = {"TWILIO_ACCOUNT_SID": "ACtest", "TWILIO_AUTH_TOKEN": "test-token"}
    monkeypatch.setattr("gptme_voice.realtime.server._get_config_env", credentials.get)

    class _RestClient:
        def __init__(self, account_sid, auth_token):
            assert (account_sid, auth_token) == ("ACtest", "test-token")

        def calls(self, call_sid):
            assert call_sid == "CAtest"
            return self

        def update(self, *, status):
            timeline.append(("rest", status))

    monkeypatch.setattr("twilio.rest.Client", _RestClient)
    server = VoiceServer(workspace=tmp_path, provider="openai")
    on_end = AsyncMock()
    monkeypatch.setattr(server, "_on_call_end", on_end)
    monkeypatch.setattr(
        server,
        "_build_session_bootstrap",
        AsyncMock(return_value=SessionBootstrap("Test")),
    )
    client = _OfflineClient()
    if prewarmed:
        monkeypatch.setattr(server, "_claim_prewarm", AsyncMock(return_value=client))
    else:
        monkeypatch.setattr(server, "_claim_prewarm", AsyncMock(return_value=None))

        def make_client(config, **kwargs):
            nonlocal client
            client = _OfflineClient(session_config=config, **kwargs)
            return client

        monkeypatch.setattr(server, "_make_client", make_client)
    websocket = _TwilioSocket(timeline)
    task = asyncio.create_task(server.handle_twilio_websocket(websocket))
    await websocket.receive_event(
        {
            "event": "start",
            "start": {
                "streamSid": "MZtest",
                "callSid": "CAtest",
                "customParameters": sign_stream_params(
                    {"from_number": "+15550000000"}, "test-token"
                ),
            },
        }
    )
    assert client.ready.is_set()
    assert not task.done(), "Twilio setup failed before the playback exercise"
    try:
        yield websocket, client, task, timeline, on_end
    finally:
        if not task.done():
            await websocket.incoming.put(({"event": "stop"}, asyncio.Event()))
        await asyncio.wait_for(task, 1)
        assert client.disconnected


async def _provider(client, event_type, **data):
    await client._handle_event({"type": event_type, **data})


async def _hangup(client):
    await _provider(
        client,
        "response.function_call_arguments.done",
        call_id="hangup-test",
        name="hangup",
        arguments="{}",
    )


async def _farewell(client):
    await _provider(
        client,
        "response.output_audio_transcript.done",
        transcript="I'll end the call now. Goodbye!",
    )


@pytest.mark.parametrize("prewarmed", [False, True])
def test_farewell_waits_for_generation_and_exact_playback_echo(
    monkeypatch, tmp_path, prewarmed
):
    async def exercise():
        async with _call(monkeypatch, tmp_path, prewarmed=prewarmed) as call:
            websocket, client, handler, timeline, on_end = call
            await _provider(
                client,
                "conversation.item.input_audio_transcription.completed",
                transcript="Please end the call now",
                item_id="goodbye",
            )
            await _provider(client, "response.created")
            await _hangup(client)
            # The tool may finish before the farewell's final audio/transcript.
            await _provider(
                client,
                "response.output_audio.delta",
                delta=base64.b64encode(b"\x00\x00" * 240).decode(),
            )
            await _farewell(client)  # Duplicate fallback shares the tool's task.
            await _provider(client, "response.output_audio.done")
            await asyncio.sleep(0)
            assert [message["event"] for message in websocket.messages] == ["media"]
            assert not any(kind == "rest" for kind, _ in timeline)
            assert not any(kind == "response.create" for kind, _ in client.sent_events)
            await _provider(client, "response.done", response={"status": "completed"})
            mark = await websocket.next_event("mark")
            await websocket.receive_event(
                {"event": "mark", "mark": {"name": "some-other-mark"}}
            )
            assert not websocket.closed
            assert not any(kind == "rest" for kind, _ in timeline)
            timeline.append(("echo", mark["mark"]["name"]))
            await websocket.receive_event({"event": "mark", "mark": mark["mark"]})
            # A successful drain adds no legacy five-second sleep.
            await asyncio.wait_for(handler, 1)
            assert [kind for kind, _ in timeline] == [
                "send",
                "send",
                "echo",
                "rest",
                "close",
            ]
            assert (
                sum(message["event"] == "mark" for message in websocket.messages) == 1
            )
            on_end.assert_awaited_once()

    asyncio.run(exercise())


def test_speech_invalidates_clear_echo_and_late_farewell_then_stop_cleans_up(
    monkeypatch, tmp_path
):
    async def exercise():
        async with _call(monkeypatch, tmp_path) as call:
            websocket, client, handler, timeline, on_end = call
            await _provider(
                client,
                "conversation.item.input_audio_transcription.completed",
                transcript="Please hang up",
                item_id="goodbye",
            )
            await _provider(client, "response.created")
            await _farewell(client)  # Transcript fallback alone schedules hangup.
            await _provider(client, "response.done", response={"status": "completed"})
            old_mark = await websocket.next_event("mark")
            await _provider(client, "input_audio_buffer.speech_started")
            assert (await websocket.next_event("clear"))["streamSid"] == "MZtest"
            await _farewell(client)  # Late transcript from the interrupted response.
            await _hangup(client)  # Late tool completion must not re-arm termination.
            await _provider(client, "response.done", response={"status": "cancelled"})
            await websocket.receive_event({"event": "mark", "mark": old_mark["mark"]})
            # A re-armed hangup would send its own mark straight away.
            await asyncio.sleep(0)
            assert sum(m["event"] == "mark" for m in websocket.messages) == 1
            assert not any(kind == "rest" for kind, _ in timeline)
            assert not websocket.closed
            await _provider(client, "response.created")
            await _hangup(client)
            await _provider(client, "response.done", response={"status": "completed"})
            new_mark = await websocket.next_event("mark")
            assert new_mark["mark"] != old_mark["mark"]
            await websocket.receive_event({"event": "mark", "mark": old_mark["mark"]})
            assert not websocket.closed
            await websocket.incoming.put(({"event": "stop"}, asyncio.Event()))
            await asyncio.wait_for(handler, 1)
            assert not [
                task
                for task in asyncio.all_tasks()
                if task.get_coro().__name__ in {"_schedule_hangup", "_drain"}
            ]
            # A late echo cannot revive the drain after transport teardown.
            await websocket.incoming.put((new_mark, asyncio.Event()))
            await asyncio.sleep(0)
            assert not any(kind == "rest" for kind, _ in timeline)
            on_end.assert_awaited_once()

    asyncio.run(exercise())


def test_provider_drain_after_stop_cannot_rearm_hangup(monkeypatch, tmp_path):
    async def exercise():
        async with _call(monkeypatch, tmp_path) as call:
            websocket, client, handler, timeline, on_end = call
            await _provider(client, "response.created")
            await _hangup(client)

            async def disconnect_with_late_provider_events(**kwargs):
                # disconnect drains provider events to preserve the transcript;
                # its callbacks remain live after Twilio has already stopped.
                await _provider(client, "response.created")
                await _hangup(client)
                await _farewell(client)
                await _provider(
                    client, "response.done", response={"status": "completed"}
                )
                client.disconnected = True

            monkeypatch.setattr(
                client, "disconnect", disconnect_with_late_provider_events
            )
            await websocket.incoming.put(({"event": "stop"}, asyncio.Event()))
            await asyncio.wait_for(handler, 1)
            assert not [
                task
                for task in asyncio.all_tasks()
                if task.get_coro().__name__ in {"_schedule_hangup", "_drain"}
            ]
            assert websocket.messages == []
            assert not any(kind == "rest" for kind, _ in timeline)
            on_end.assert_awaited_once()

    asyncio.run(exercise())
