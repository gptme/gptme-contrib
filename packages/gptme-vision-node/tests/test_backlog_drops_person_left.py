"""Under transport backlog, the bridge drops events indiscriminately; a
person_left lost behind motion events leaves the host believing someone is
present forever (the pipeline is edge-triggered and never re-sends it)."""

from __future__ import annotations

import asyncio
import json

import pytest
from gptme_vision_node.bridge import VisionBridge
from gptme_vision_node.pipeline import MOTION, PERSON_APPEARED, PERSON_LEFT, VisionEvent


class _Source:
    def get_frame(self):
        return None

    def release(self):
        pass


@pytest.mark.asyncio
async def test_person_left_survives_backlog() -> None:
    bridge = VisionBridge(_Source(), [])
    gate = asyncio.Event()
    delivered: list[str] = []

    async def slow_send(message: str) -> None:
        await gate.wait()  # congested uplink
        delivered.append(json.loads(message)["event"])

    bridge._loop = asyncio.get_running_loop()
    bridge._send_message = slow_send
    bridge._schedule_event(VisionEvent(PERSON_APPEARED))
    for _ in range(3):  # person walking out of frame -> motion every frame
        bridge._schedule_event(VisionEvent(MOTION))
    bridge._schedule_event(VisionEvent(PERSON_LEFT))

    gate.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert PERSON_APPEARED in delivered
    assert PERSON_LEFT in delivered, f"delivered={delivered}"
    await bridge.stop()
