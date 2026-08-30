"""Tests for the edge-side camera/voice bridge."""

from __future__ import annotations

import asyncio
import base64
import json
import threading

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
async def test_event_send_queue_is_bounded(caplog) -> None:
    bridge = VisionBridge(_Source(), [])
    unblock = asyncio.Event()

    async def blocked_send(_message: str) -> None:
        await unblock.wait()

    bridge._loop = asyncio.get_running_loop()
    bridge._send_message = blocked_send
    event = VisionEvent(PERSON_APPEARED)
    for _ in range(10):
        bridge._schedule_event(event)

    assert len(bridge._pending_sends) == 4
    assert "transport is backlogged" in caplog.text
    unblock.set()
    await bridge.stop()


@pytest.mark.asyncio
async def test_stop_cancels_blocked_event_sends() -> None:
    bridge = VisionBridge(_Source(), [])
    blocked = asyncio.Event()

    async def blocked_send(_message: str) -> None:
        await blocked.wait()

    bridge._send_message = blocked_send
    bridge._schedule_event(VisionEvent(PERSON_APPEARED))

    await asyncio.wait_for(bridge.stop(), timeout=0.1)
    assert not bridge._pending_sends


@pytest.mark.asyncio
async def test_stop_releases_source_after_pipeline_exits() -> None:
    source = _Source()
    bridge = VisionBridge(source, [])
    await bridge.stop()
    assert source.released


@pytest.mark.asyncio
async def test_stop_skips_release_while_capture_thread_is_blocked() -> None:
    unblock = threading.Event()
    entered = threading.Event()

    class BlockingSource:
        def __init__(self) -> None:
            self.released = False

        def get_frame(self):
            entered.set()
            unblock.wait()
            return None

        def release(self) -> None:
            self.released = True

    source = BlockingSource()
    bridge = VisionBridge(source, [])

    async def send(_message: str) -> None:
        return None

    bridge.start(asyncio.get_running_loop(), send)
    assert entered.wait(timeout=1)
    try:
        await asyncio.wait_for(bridge.stop(timeout=0.05), timeout=1)
        assert not source.released
        assert bridge.pipeline.is_running()
    finally:
        unblock.set()
        thread = bridge.pipeline._thread
        if thread is not None:
            thread.join(timeout=1)
        await bridge.stop()
    assert source.released


@pytest.mark.asyncio
async def test_stop_cleans_up_when_cancelled_during_join() -> None:
    """A cancelled stop() must still drain pending sends and skip unsafe release."""
    unblock = threading.Event()
    entered = threading.Event()

    class BlockingSource:
        def __init__(self) -> None:
            self.released = False

        def get_frame(self):
            entered.set()
            unblock.wait()
            return None

        def release(self) -> None:
            self.released = True

    source = BlockingSource()
    bridge = VisionBridge(source, [])
    hold_send = asyncio.Event()

    async def blocked_send(_message: str) -> None:
        await hold_send.wait()

    bridge.start(asyncio.get_running_loop(), blocked_send)
    assert entered.wait(timeout=1)
    bridge._schedule_event(VisionEvent(PERSON_APPEARED))
    assert bridge._pending_sends

    stop_task = asyncio.create_task(bridge.stop(timeout=0.2))
    await asyncio.sleep(0)
    stop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop_task

    assert not bridge._pending_sends
    assert not source.released
    assert bridge.pipeline.is_running()

    unblock.set()
    thread = bridge.pipeline._thread
    if thread is not None:
        thread.join(timeout=1)
    await bridge.stop()
    assert source.released


@pytest.mark.asyncio
async def test_unrelated_server_message_is_not_consumed() -> None:
    bridge = VisionBridge(_Source(), [])

    async def send(_message: str) -> None:
        raise AssertionError("must not send")

    assert not await bridge.handle_message({"type": "audio_end"}, send)
