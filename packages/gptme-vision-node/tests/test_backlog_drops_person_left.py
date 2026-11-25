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


@pytest.mark.asyncio
async def test_latest_presence_survives_sustained_backlog() -> None:
    bridge = VisionBridge(_Source(), [])
    gate = asyncio.Event()
    delivered: list[str] = []

    async def slow_send(message: str) -> None:
        await gate.wait()
        delivered.append(json.loads(message)["event"])

    bridge._loop = asyncio.get_running_loop()
    bridge._send_message = slow_send
    for i in range(17):
        kind = PERSON_APPEARED if i % 2 == 0 else PERSON_LEFT
        bridge._schedule_event(VisionEvent(kind))
    assert len(bridge._pending_sends) <= 16
    gate.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert delivered[-1] == PERSON_APPEARED
    await bridge.stop()


@pytest.mark.asyncio
async def test_stop_discards_deferred_presence() -> None:
    bridge = VisionBridge(_Source(), [])
    gate = asyncio.Event()
    delivered: list[str] = []

    async def slow_send(message: str) -> None:
        await gate.wait()
        delivered.append(json.loads(message)["event"])

    bridge._loop = asyncio.get_running_loop()
    bridge._send_message = slow_send
    for _ in range(17):
        bridge._schedule_event(VisionEvent(PERSON_APPEARED))
    await asyncio.sleep(0)
    await bridge.stop()
    bridge._loop = asyncio.get_running_loop()
    bridge._send_message = slow_send
    gate.set()
    bridge._schedule_event(VisionEvent(PERSON_LEFT))
    for _ in range(10):
        await asyncio.sleep(0)
    assert delivered == [PERSON_LEFT]
    await bridge.stop()


@pytest.mark.asyncio
async def test_motion_cannot_starve_deferred_presence() -> None:
    bridge = VisionBridge(_Source(), [])
    gates = [asyncio.Event() for _ in range(16)]
    started = 0
    delivered: list[str] = []

    async def slow_send(message: str) -> None:
        nonlocal started
        index = started
        started += 1
        if index < len(gates):
            await gates[index].wait()
        delivered.append(json.loads(message)["event"])

    bridge._loop = asyncio.get_running_loop()
    bridge._send_message = slow_send
    for _ in range(16):
        bridge._schedule_event(VisionEvent(PERSON_APPEARED))
    bridge._schedule_event(VisionEvent(PERSON_LEFT))
    await asyncio.sleep(0)
    for gate in gates[:13]:
        gate.set()
    for _ in range(3):
        await asyncio.sleep(0)
    assert len(bridge._pending_sends) == 3
    bridge._schedule_event(VisionEvent(MOTION))
    assert len(bridge._pending_sends) == 3
    for gate in gates[13:]:
        gate.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert delivered[-1] == PERSON_LEFT
    assert MOTION not in delivered
    await bridge.stop()
