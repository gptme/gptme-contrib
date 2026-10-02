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
    # Use a model that is NOT the xAI default to verify passthrough is genuine
    explicit_model = "grok-voice-think-1.0"
    cfg = SessionConfig(model=explicit_model)
    client = XAIRealtimeClient(api_key="test-key", session_config=cfg)

    assert client.session_config.model == explicit_model
    assert client._get_ws_url() == f"wss://api.x.ai/v1/realtime?model={explicit_model}"


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


def test_xai_client_ignores_openai_reasoning_config() -> None:
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
                    reasoning_effort="low",
                ),
            )
            await client.connect()
            await asyncio.sleep(0)
            await client.disconnect()

        session_update = fake_ws.sent[0]
        assert "reasoning" not in session_update["session"]

    asyncio.run(_exercise())


def test_xai_session_update_keeps_legacy_flat_shape() -> None:
    """xAI is production on the flat (beta-style) shape — it must not change
    when the OpenAI client moves to the GA session shape."""

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
                    output_speed=1.1,
                    available_agents=[],
                ),
            )
            await client.connect()
            await client.disconnect()

        session = fake_ws.sent[0]["session"]
        assert {k: v for k, v in session.items() if k != "tools"} == {
            "modalities": ["text", "audio"],
            "instructions": "You are Bob.",
            "voice": "rex",
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.55,
                "silence_duration_ms": 500,
                "prefix_padding_ms": 150,
            },
            "output": {"speed": 1.1},
        }
        assert [t["name"] for t in session["tools"]] == [
            "subagent",
            "subagent_status",
            "subagent_cancel",
            "hangup",
        ]

    asyncio.run(_exercise())


def test_xai_g711_passthrough_keeps_legacy_format_strings() -> None:
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
                session_config=SessionConfig(g711_passthrough=True),
            )
            await client.connect()
            await client.disconnect()

        session = fake_ws.sent[0]["session"]
        assert session["input_audio_format"] == "g711_ulaw"
        assert session["output_audio_format"] == "g711_ulaw"
        assert "audio" not in session
        assert "type" not in session

    asyncio.run(_exercise())


def test_xai_still_treats_session_created_as_ready() -> None:
    async def _exercise() -> None:
        fake_ws = _FakeWebSocket()

        async def _fake_connect(*_args, **_kwargs):
            return fake_ws

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "gptme_voice.realtime.openai_client.websockets.connect", _fake_connect
            )
            client = XAIRealtimeClient(api_key="test-key")
            await client.connect()
            await client._handle_event({"type": "session.created"})
            assert client._session_ready is not None
            assert client._session_ready.is_set()
            await client.disconnect()

    asyncio.run(_exercise())


def test_xai_error_keeps_historical_log_only_behaviour() -> None:
    async def _exercise() -> None:
        fake_ws = _FakeWebSocket()

        async def _fake_connect(*_args, **_kwargs):
            return fake_ws

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "gptme_voice.realtime.openai_client.websockets.connect", _fake_connect
            )
            client = XAIRealtimeClient(api_key="test-key")
            await client.connect()
            await client._handle_event(
                {"type": "error", "error": {"message": "x", "param": "session.foo"}}
            )
            assert client.session_error is None
            assert fake_ws.closed is False
            await client.disconnect()

    asyncio.run(_exercise())
