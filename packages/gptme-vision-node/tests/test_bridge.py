"""Tests for the edge-side camera/voice bridge."""

from __future__ import annotations

import asyncio
import base64
import json

import numpy as np
import pytest
from gptme_vision_node.bridge import VisionBridge
from gptme_vision_node.detect import Detection
from gptme_vision_node.pipeline import PERSON_APPEARED, VisionEvent


class _Source:
    def __init__(self) -> None:
        self.released = False

    def get_frame(self):
        return None

    def release(self) -> None:
        self.released = True


@pytest.mark.asyncio
async def test_event_callback_crosses_into_websocket_loop() -> None:
    bridge = VisionBridge(_Source(), [])
    sent: list[str] = []

    async def send(message: str) -> None:
        sent.append(message)

    bridge._loop = asyncio.get_running_loop()
    bridge._send_message = send
    bridge._on_event(
        VisionEvent(
            PERSON_APPEARED,
            (Detection("person", (1, 2, 3, 4), 0.9),),
            timestamp=123.0,
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert json.loads(sent[0]) == {
        "type": "vision_event",
        "event": "person_appeared",
        "timestamp": 123.0,
        "detections": [{"kind": "person", "box": [1, 2, 3, 4], "score": 0.9}],
    }


@pytest.mark.asyncio
async def test_look_request_returns_latest_frame_as_jpeg() -> None:
    bridge = VisionBridge(_Source(), [])
    bridge.pipeline.latest_frame = np.full((20, 20, 3), 127, dtype=np.uint8)
    sent: list[str] = []

    async def send(message: str) -> None:
        sent.append(message)

    handled = await bridge.handle_message(
        {"type": "vision_look_request", "request_id": "req-1"}, send
    )

    assert handled is True
    payload = json.loads(sent[0])
    assert payload["type"] == "vision_look_result"
    assert payload["request_id"] == "req-1"
    assert payload["media_type"] == "image/jpeg"
    assert base64.b64decode(payload["image"])[:2] == b"\xff\xd8"


@pytest.mark.asyncio
async def test_look_before_first_frame_returns_bounded_error() -> None:
    bridge = VisionBridge(_Source(), [])
    sent: list[str] = []

    async def send(message: str) -> None:
        sent.append(message)

    assert await bridge.handle_message(
        {"type": "vision_look_request", "request_id": "req-2"}, send
    )
    assert json.loads(sent[0]) == {
        "type": "vision_look_result",
        "request_id": "req-2",
        "error": "Camera has not produced a frame yet.",
    }


@pytest.mark.asyncio
async def test_unrelated_server_message_is_not_consumed() -> None:
    bridge = VisionBridge(_Source(), [])

    async def send(_message: str) -> None:
        raise AssertionError("must not send")

    assert not await bridge.handle_message({"type": "audio_end"}, send)
