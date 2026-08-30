"""Tests for the host side of the BobBrain vision/voice bridge."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from gptme_voice import vision
from gptme_voice.vision import VisionSessionBridge, describe_image, vision_tool_schema


def test_vision_tool_schema_is_an_on_demand_look() -> None:
    schema = vision_tool_schema()
    assert schema["name"] == "look"
    assert schema["parameters"]["properties"]["prompt"]["type"] == "string"


@pytest.mark.asyncio
async def test_look_roundtrip_requests_frame_then_describes_it() -> None:
    sent: list[dict] = []
    described: list[tuple[bytes, str, str]] = []

    async def send(message: str) -> None:
        payload = json.loads(message)
        sent.append(payload)
        await bridge.handle_message(
            {
                "type": "vision_look_result",
                "request_id": payload["request_id"],
                "image": base64.b64encode(b"jpeg-bytes").decode(),
                "media_type": "image/jpeg",
            }
        )

    async def describe(image: bytes, prompt: str, *, media_type: str) -> str:
        described.append((image, prompt, media_type))
        return "Erik is standing by the table."

    bridge = VisionSessionBridge(send, describe=describe)
    result = await bridge.look("Who is in front of me?")

    assert sent[0]["type"] == "vision_look_request"
    assert result == {
        "status": "ok",
        "description": "Erik is standing by the table.",
    }
    assert described == [(b"jpeg-bytes", "Who is in front of me?", "image/jpeg")]


@pytest.mark.asyncio
async def test_look_times_out_without_a_camera_result() -> None:
    async def send(_message: str) -> None:
        return None

    bridge = VisionSessionBridge(send, look_timeout_s=0.01)
    result = await bridge.look()

    assert result == {"error": "Camera did not return a frame before the timeout."}
    assert bridge._pending == {}


@pytest.mark.asyncio
async def test_vision_event_reaches_callback_without_triggering_vlm() -> None:
    events: list[str] = []

    async def send(_message: str) -> None:
        return None

    async def on_event(event: str) -> None:
        events.append(event)

    bridge = VisionSessionBridge(send, on_event=on_event)
    assert await bridge.handle_message(
        {"type": "vision_event", "event": "person_appeared"}
    )
    assert events == ["person_appeared"]


@pytest.mark.asyncio
async def test_look_returns_bounded_error_for_invalid_vision_url() -> None:
    async def send(message: str) -> None:
        payload = json.loads(message)
        await bridge.handle_message(
            {
                "type": "vision_look_result",
                "request_id": payload["request_id"],
                "image": base64.b64encode(b"jpeg-bytes").decode(),
                "media_type": "image/jpeg",
            }
        )

    async def invalid_url(*_args, **_kwargs) -> str:
        raise httpx.InvalidURL("bad vision URL")

    bridge = VisionSessionBridge(send, describe=invalid_url)

    assert await bridge.look() == {
        "error": "Could not inspect the camera frame: bad vision URL"
    }


@pytest.mark.asyncio
async def test_describe_image_uses_async_openai_compatible_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "a room"}}]}

    class _Client:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url: str, *, json: dict, headers: dict):
            captured.update(url=url, body=json, headers=headers)
            return _Response()

    class _Config:
        values = {
            "OPENAI_API_KEY": "sk-config",
            "OPENAI_BASE_URL": "https://vision.test/v1/",
            "VISION_MODEL": "vision-config-model",
        }

        def get_env(self, key: str) -> str | None:
            return self.values.get(key)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(vision, "get_config", _Config)
    result = await describe_image(b"jpeg", "what?")

    assert result == "a room"
    assert captured["url"] == "https://vision.test/v1/chat/completions"
    assert captured["headers"] == {"Authorization": "Bearer sk-config"}
    assert captured["body"]["model"] == "vision-config-model"
    image_url = captured["body"]["messages"][0]["content"][1]["image_url"]["url"]
    assert image_url == "data:image/jpeg;base64,anBlZw=="
