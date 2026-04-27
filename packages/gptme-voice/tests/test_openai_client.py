import asyncio
import base64
import json
from pathlib import Path

import pytest
from gptme_voice.realtime.openai_client import (
    _MAX_PENDING_AUDIO_CHUNKS,
    OpenAIRealtimeClient,
    SessionConfig,
    _load_project_instructions,
)


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def __aiter__(self) -> "_FakeWebSocket":
        return self

    async def __anext__(self) -> str:
        raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True


class _QueuedWebSocket(_FakeWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self._incoming: asyncio.Queue[str | None] = asyncio.Queue()

    async def feed(self, event: dict) -> None:
        await self._incoming.put(json.dumps(event))

    def __aiter__(self) -> "_QueuedWebSocket":
        return self

    async def __anext__(self) -> str:
        message = await self._incoming.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            await self._incoming.put(None)


def test_connect_exposes_subagent_tool_as_focused_lookup_only() -> None:
    async def _exercise() -> None:
        fake_ws = _FakeWebSocket()

        async def _fake_connect(*_args, **_kwargs):
            return fake_ws

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "gptme_voice.realtime.openai_client.websockets.connect", _fake_connect
            )
            client = OpenAIRealtimeClient(
                api_key="test-key",
                session_config=SessionConfig(instructions="You are Bob."),
            )
            await client.connect()
            await asyncio.sleep(0)
            await client.disconnect()

        session_update = fake_ws.sent[0]
        tool = session_update["session"]["tools"][0]
        description = tool["description"]

        assert session_update["type"] == "session.update"
        assert tool["name"] == "subagent"
        assert "small, focused workspace lookup or action" in description
        assert "broad investigations" in description
        assert "post-call analysis" in description
        assert "wait for the real subagent result" in description
        assert fake_ws.closed is True

    asyncio.run(_exercise())


def test_load_project_instructions_includes_post_call_follow_up_guard(
    tmp_path: Path,
) -> None:
    """Preamble must tell the live-call model not to claim post-call dispatch itself."""
    (tmp_path / "gptme.toml").write_text(
        '[prompt]\nfiles = ["ABOUT.md"]\n',
    )
    (tmp_path / "ABOUT.md").write_text("# ABOUT\nYou are Bob.\n")

    instructions = _load_project_instructions(str(tmp_path))

    # The preamble was applied (sanity — otherwise we'd get _DEFAULT_INSTRUCTIONS).
    assert "real-time voice conversation" in instructions
    assert "wait for the actual subagent result" in instructions

    # The new POST-CALL FOLLOW-UP section exists.
    assert "POST-CALL FOLLOW-UP:" in instructions

    # The core behavioral constraint from the 2026-04-20 call is present:
    # do not verbally claim to have dispatched post-call work during the call.
    assert "Do NOT claim, announce, or imply that you have dispatched" in instructions
    assert "post-call analysis dispatched" in instructions

    # Acknowledging automatic post-call follow-up is still allowed.
    assert "happen automatically after" in instructions


def test_send_audio_buffers_until_session_created() -> None:
    """Audio arriving before session.created must be buffered, not forwarded.

    Regression for silent-call-on-startup: Twilio media frames can arrive
    before the provider confirms the session with ``session.created``. Sending
    audio before then is a no-op on the provider side — the caller hears
    silence because Grok has not started listening.
    """

    async def _exercise() -> None:
        fake_ws = _FakeWebSocket()

        async def _fake_connect(*_args, **_kwargs):
            return fake_ws

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "gptme_voice.realtime.openai_client.websockets.connect", _fake_connect
            )
            client = OpenAIRealtimeClient(api_key="test-key")
            await client.connect()

            # connect() sends session.update; everything after index 0 is
            # what send_audio / flush produced.
            assert fake_ws.sent[0]["type"] == "session.update"
            baseline = len(fake_ws.sent)

            # Audio before session.created must be buffered, not sent.
            await client.send_audio(b"\x01\x02\x03")
            await client.send_audio(b"\x04\x05\x06")
            assert len(fake_ws.sent) == baseline
            assert len(client._pending_audio) == 2

            # Simulate provider confirming the session — the flush should
            # replay buffered chunks in order.
            await client._handle_event({"type": "session.created"})

            assert client._session_ready is not None
            assert client._session_ready.is_set()
            assert client._pending_audio == []

            appends = [
                e for e in fake_ws.sent if e.get("type") == "input_audio_buffer.append"
            ]
            assert len(appends) == 2
            assert base64.b64decode(appends[0]["audio"]) == b"\x01\x02\x03"
            assert base64.b64decode(appends[1]["audio"]) == b"\x04\x05\x06"

            # After ready, send_audio goes straight through.
            await client.send_audio(b"\x07\x08")
            appends = [
                e for e in fake_ws.sent if e.get("type") == "input_audio_buffer.append"
            ]
            assert len(appends) == 3
            assert base64.b64decode(appends[2]["audio"]) == b"\x07\x08"

            await client.disconnect()

    asyncio.run(_exercise())


def test_send_audio_buffer_is_bounded() -> None:
    """Buffer must be capped so a never-arriving session.created cannot leak memory."""

    async def _exercise() -> None:
        fake_ws = _FakeWebSocket()

        async def _fake_connect(*_args, **_kwargs):
            return fake_ws

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "gptme_voice.realtime.openai_client.websockets.connect", _fake_connect
            )
            client = OpenAIRealtimeClient(api_key="test-key")
            await client.connect()

            # Fill the buffer to capacity plus 3 overflow chunks.
            for i in range(_MAX_PENDING_AUDIO_CHUNKS + 3):
                await client.send_audio(bytes([i % 256]))

            assert len(client._pending_audio) == _MAX_PENDING_AUDIO_CHUNKS
            assert client._pending_audio_dropped == 3

            await client.disconnect()

    asyncio.run(_exercise())


def test_initial_response_is_sent_once_after_session_ready() -> None:
    async def _exercise() -> None:
        fake_ws = _FakeWebSocket()

        async def _fake_connect(*_args, **_kwargs):
            return fake_ws

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "gptme_voice.realtime.openai_client.websockets.connect", _fake_connect
            )
            client = OpenAIRealtimeClient(
                api_key="test-key",
                session_config=SessionConfig(
                    initial_response_instructions="Greet the caller briefly.",
                ),
            )
            await client.connect()

            await client._handle_event({"type": "session.created"})
            await client._handle_event({"type": "session.updated"})

            response_creates = [
                event
                for event in fake_ws.sent
                if event.get("type") == "response.create"
            ]
            assert response_creates == [
                {
                    "type": "response.create",
                    "response": {"instructions": "Greet the caller briefly."},
                }
            ]

            await client.disconnect()

    asyncio.run(_exercise())


def test_load_project_instructions_guards_present_without_personality_files(
    tmp_path: Path,
) -> None:
    """Guards must apply even when a workspace has no matching personality files."""
    # Config exists but references a file that doesn't exist — no parts loaded.
    (tmp_path / "gptme.toml").write_text(
        '[prompt]\nfiles = ["MISSING.md"]\n',
    )

    instructions = _load_project_instructions(str(tmp_path))

    # Should NOT fall back to the bare _DEFAULT_INSTRUCTIONS.
    assert "You are a helpful assistant" not in instructions

    # Behavioral guards must still be present.
    assert "real-time voice conversation" in instructions
    assert "POST-CALL FOLLOW-UP:" in instructions
    assert "Do NOT claim, announce, or imply that you have dispatched" in instructions
    assert "wait for the actual subagent result" in instructions


def test_function_call_subagent_dispatch_does_not_auto_create_response() -> None:
    async def _exercise() -> None:
        fake_ws = _FakeWebSocket()

        async def _fake_connect(*_args, **_kwargs):
            return fake_ws

        async def _on_function_call(name: str, arguments: dict) -> dict:
            assert name == "subagent"
            assert arguments == {"task": "check latest standup"}
            return {
                "status": "dispatched",
                "task_id": "task-1",
                "message": "Async lookup dispatched. Wait for the subagent result before giving the substantive answer.",
            }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "gptme_voice.realtime.openai_client.websockets.connect", _fake_connect
            )
            client = OpenAIRealtimeClient(
                api_key="test-key",
                on_function_call=_on_function_call,
            )
            await client.connect()
            await client._handle_event({"type": "session.created"})

            baseline = len(fake_ws.sent)
            await client._handle_event(
                {
                    "type": "response.function_call_arguments.done",
                    "call_id": "call-1",
                    "name": "subagent",
                    "arguments": json.dumps({"task": "check latest standup"}),
                }
            )

            new_events = fake_ws.sent[baseline:]
            assert new_events == [
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": "call-1",
                        "output": json.dumps(
                            {
                                "status": "dispatched",
                                "task_id": "task-1",
                                "message": "Async lookup dispatched. Wait for the subagent result before giving the substantive answer.",
                            }
                        ),
                    },
                }
            ]

            await client.disconnect()

    asyncio.run(_exercise())


def test_function_call_subagent_status_still_auto_creates_response() -> None:
    async def _exercise() -> None:
        fake_ws = _FakeWebSocket()

        async def _fake_connect(*_args, **_kwargs):
            return fake_ws

        async def _on_function_call(name: str, arguments: dict) -> dict:
            assert name == "subagent_status"
            assert arguments == {}
            return {
                "status": "ok",
                "pending_count": 1,
                "pending": [
                    {
                        "task_id": "task-1",
                        "task": "check latest standup",
                        "mode": "fast",
                        "elapsed_seconds": 2.3,
                        "stage": "running",
                    }
                ],
            }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "gptme_voice.realtime.openai_client.websockets.connect", _fake_connect
            )
            client = OpenAIRealtimeClient(
                api_key="test-key",
                on_function_call=_on_function_call,
            )
            await client.connect()
            await client._handle_event({"type": "session.created"})

            baseline = len(fake_ws.sent)
            await client._handle_event(
                {
                    "type": "response.function_call_arguments.done",
                    "call_id": "call-2",
                    "name": "subagent_status",
                    "arguments": json.dumps({}),
                }
            )

            new_events = fake_ws.sent[baseline:]
            assert new_events == [
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": "call-2",
                        "output": json.dumps(
                            {
                                "status": "ok",
                                "pending_count": 1,
                                "pending": [
                                    {
                                        "task_id": "task-1",
                                        "task": "check latest standup",
                                        "mode": "fast",
                                        "elapsed_seconds": 2.3,
                                        "stage": "running",
                                    }
                                ],
                            }
                        ),
                    },
                },
                {"type": "response.create"},
            ]

            await client.disconnect()

    asyncio.run(_exercise())


def test_disconnect_drains_late_transcript_events_without_sending_late_audio() -> None:
    """Call teardown should preserve late transcript events but suppress late audio.

    Regression for Twilio calls that hang up while the provider is still about to
    emit the final transcript turn. We want the text for persistence, but we must
    not try to write late audio to a websocket that is already closing.
    """

    async def _exercise() -> None:
        fake_ws = _QueuedWebSocket()
        user_transcripts: list[str] = []
        audio_chunks: list[bytes] = []

        async def _fake_connect(*_args, **_kwargs):
            return fake_ws

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "gptme_voice.realtime.openai_client.websockets.connect", _fake_connect
            )
            client = OpenAIRealtimeClient(
                api_key="test-key",
                on_user_transcript=user_transcripts.append,
                on_audio=audio_chunks.append,
            )
            await client.connect()
            await client._handle_event({"type": "session.created"})

            async def _emit_late_events() -> None:
                await asyncio.sleep(0.01)
                await fake_ws.feed(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "transcript": "final words",
                    }
                )
                await fake_ws.feed(
                    {
                        "type": "response.audio.delta",
                        "delta": base64.b64encode(b"\x01\x02").decode("utf-8"),
                    }
                )

            emit_task = asyncio.create_task(_emit_late_events())
            await client.disconnect(
                drain_timeout_seconds=0.2,
                idle_timeout_seconds=0.05,
                commit_audio=True,
                stop_audio_output=True,
            )
            await emit_task

        commit_events = [
            event
            for event in fake_ws.sent
            if event.get("type") == "input_audio_buffer.commit"
        ]
        assert commit_events, "disconnect should commit pending audio before drain"
        assert user_transcripts == ["final words"]
        assert audio_chunks == []
        assert fake_ws.closed is True

    asyncio.run(_exercise())
