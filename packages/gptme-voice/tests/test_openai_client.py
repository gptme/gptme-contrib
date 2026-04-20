import asyncio
import json

import pytest
from gptme_voice.realtime.openai_client import OpenAIRealtimeClient, SessionConfig


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
        assert fake_ws.closed is True

    asyncio.run(_exercise())
