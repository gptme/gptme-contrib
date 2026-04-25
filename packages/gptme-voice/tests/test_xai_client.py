import asyncio
import base64
import json

import pytest
from gptme_voice.realtime.openai_client import SessionConfig
from gptme_voice.realtime.xai_client import XAIRealtimeClient


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


def test_xai_client_uses_xai_defaults() -> None:
    client = XAIRealtimeClient(api_key="test-key", session_config=SessionConfig())

    assert client.session_config.voice == "rex"  # male voice for Bob persona
    assert client.session_config.model == "grok-voice-think-fast-1.0"
    assert client.session_config.vad_threshold == 0.55
    assert client.session_config.vad_silence_duration_ms == 500
    assert client.session_config.vad_prefix_padding_ms == 150
    assert (
        client._get_ws_url()
        == "wss://api.x.ai/v1/realtime?model=grok-voice-think-fast-1.0"
    )


def test_xai_client_respects_explicit_model() -> None:
    cfg = SessionConfig(model="grok-voice-think-fast-1.0")
    client = XAIRealtimeClient(api_key="test-key", session_config=cfg)

    assert client.session_config.model == "grok-voice-think-fast-1.0"
    assert (
        client._get_ws_url()
        == "wss://api.x.ai/v1/realtime?model=grok-voice-think-fast-1.0"
    )


def test_xai_client_treats_session_updated_as_ready_signal() -> None:
    async def _exercise() -> None:
        fake_ws = _FakeWebSocket()

        async def _fake_connect(*_args, **_kwargs):
            return fake_ws

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "gptme_voice.realtime.openai_client.websockets.connect", _fake_connect
            )
            client = XAIRealtimeClient(
                api_key="test-key",
                session_config=SessionConfig(
                    instructions="You are Bob.",
                    initial_response_instructions="Say hello first.",
                ),
            )
            await client.connect()

            await client.send_audio(b"\x01\x02\x03")
            assert client._session_ready is not None
            assert not client._session_ready.is_set()

            await client._handle_event({"type": "session.updated"})

            assert client._session_ready.is_set()
            appends = [
                event
                for event in fake_ws.sent
                if event.get("type") == "input_audio_buffer.append"
            ]
            assert len(appends) == 1
            assert base64.b64decode(appends[0]["audio"]) == b"\x01\x02\x03"
            response_creates = [
                event
                for event in fake_ws.sent
                if event.get("type") == "response.create"
            ]
            assert response_creates == [
                {
                    "type": "response.create",
                    "response": {"instructions": "Say hello first."},
                }
            ]

            await client.disconnect()
            assert fake_ws.closed is True

    asyncio.run(_exercise())
