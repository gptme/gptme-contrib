"""Once a live camera stops producing frames, look keeps returning the last
good frame forever as if it were the current view."""

from __future__ import annotations

import asyncio
import json
import threading

import numpy as np
import pytest
from gptme_vision_node.bridge import VisionBridge


class DyingCamera:
    """Live source: one good frame, then the camera goes away for good."""

    stop_on_empty = False

    def __init__(self) -> None:
        self.calls = 0
        self.failed_reads = threading.Event()

    def get_frame(self):
        self.calls += 1
        if self.calls == 1:
            return np.full((32, 32, 3), 200, dtype=np.uint8)
        if self.calls > 20:
            self.failed_reads.set()
        return None  # OpenCVCameraSource.get_frame on an unplugged camera

    def release(self) -> None:
        pass


@pytest.mark.asyncio
async def test_look_after_camera_dies_is_not_a_stale_image() -> None:
    source = DyingCamera()
    bridge = VisionBridge(source, [], interval_s=0.005)
    sent: list[str] = []

    async def send(message: str) -> None:
        sent.append(message)

    bridge.start(asyncio.get_running_loop(), send)
    try:
        # ~20 consecutive empty reads: the camera has been dead for 20 intervals.
        assert await asyncio.to_thread(source.failed_reads.wait, 2)
        await bridge.handle_message(
            {"type": "vision_look_request", "request_id": "r"}, send
        )
    finally:
        await bridge.stop()
    payload = json.loads(sent[-1])
    assert "image" not in payload, (
        "look returned the last frame captured before the camera stopped "
        "producing frames, presented as the current view"
    )
