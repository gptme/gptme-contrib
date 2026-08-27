"""Body adapter protocol — the Brain↔Body contract.

BobBrain design rule (bobbrain-spec, ErikBjare/bob#730): the brain emits
*goals* (goto / move / turn / stop / dock), never control loops. A per-body
adapter translates goals into the body's protocol and owns safety. The
autopilot's own failsafes (e.g. PX4 offboard/link-loss behavior) remain the
final authority — a dropped brain link must always leave the body safe.

Capabilities gate which voice tools get registered for a session:
a tabletop puck (NullAdapter, no capabilities) exposes no motion tools at
all, so the model cannot try to fly a desk ornament.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Capability names
CAP_MOVE = "move"  # horizontal translation (goto/move/return home)
CAP_ROTATE = "rotate"  # yaw control
CAP_ALTITUDE = "altitude"  # vertical control (takeoff/land/up-down)


@runtime_checkable
class BodyAdapter(Protocol):
    """Minimal protocol every body implements.

    Methods for capabilities an adapter does not advertise are never
    called by the tool bridge; implementations may raise for them.
    """

    capabilities: set[str]
    name: str

    async def ensure_connected(self) -> None: ...

    async def takeoff(self, altitude_m: float) -> dict[str, Any]: ...

    async def land(self) -> dict[str, Any]: ...

    async def goto(
        self,
        latitude_deg: float,
        longitude_deg: float,
        altitude_m: float | None,
    ) -> dict[str, Any]: ...

    async def move(
        self, forward_m: float, right_m: float, up_m: float
    ) -> dict[str, Any]: ...

    async def turn(self, yaw_deg: float) -> dict[str, Any]: ...

    async def stop(self) -> dict[str, Any]: ...

    async def return_home(self) -> dict[str, Any]: ...

    def telemetry(self) -> dict[str, Any]: ...


class NullAdapter:
    """Body with no locomotion — the tabletop puck.

    Registers only ``body_status``; every motion method reports
    unsupported (defense in depth — the bridge never routes these).
    """

    capabilities: set[str] = set()
    name = "null"

    async def ensure_connected(self) -> None:
        return None

    def telemetry(self) -> dict[str, Any]:
        return {"body": self.name, "mobile": False}

    async def _unsupported(self) -> dict[str, Any]:
        return {"error": "This body has no locomotion."}

    async def takeoff(self, altitude_m: float) -> dict[str, Any]:
        return await self._unsupported()

    async def land(self) -> dict[str, Any]:
        return await self._unsupported()

    async def goto(
        self,
        latitude_deg: float,
        longitude_deg: float,
        altitude_m: float | None,
    ) -> dict[str, Any]:
        return await self._unsupported()

    async def move(
        self, forward_m: float, right_m: float, up_m: float
    ) -> dict[str, Any]:
        return await self._unsupported()

    async def turn(self, yaw_deg: float) -> dict[str, Any]:
        return await self._unsupported()

    async def stop(self) -> dict[str, Any]:
        return await self._unsupported()

    async def return_home(self) -> dict[str, Any]:
        return await self._unsupported()


def body_tool_schemas(adapter: BodyAdapter | None) -> list[dict]:
    """Realtime-API function schemas for the adapter's capability set.

    Returns [] when no adapter is configured, so callers can always
    ``extend()`` the session tool list unconditionally.
    """
    if adapter is None:
        return []

    caps = adapter.capabilities
    tools: list[dict] = [
        {
            "type": "function",
            "name": "body_status",
            "description": (
                "Read the physical body's current state: position, altitude, "
                "battery, heading, flight mode. Use it before motion commands "
                "and whenever the caller asks where the body is or how it is "
                "doing."
            ),
            "parameters": {"type": "object", "properties": {}},
        }
    ]

    if CAP_MOVE in caps:
        tools.append(
            {
                "type": "function",
                "name": "body_stop",
                "description": (
                    "IMMEDIATELY halt all body motion and hold position. "
                    "Call this first, without asking, whenever the caller "
                    "says stop/wait/freeze or anything sounds wrong."
                ),
                "parameters": {"type": "object", "properties": {}},
            }
        )
        tools.append(
            {
                "type": "function",
                "name": "body_return_home",
                "description": (
                    "Send the body back to its home/launch position "
                    "(quadcopter: return-to-launch and land)."
                ),
                "parameters": {"type": "object", "properties": {}},
            }
        )
        tools.append(
            {
                "type": "function",
                "name": "body_move",
                "description": (
                    "Move the body a relative distance in meters from where "
                    "it is now, in its own frame: forward/backward, "
                    "right/left, up/down. Use small distances (1-5 m) and "
                    "check body_status after."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "forward_m": {
                            "type": "number",
                            "description": "Meters forward (negative = backward).",
                        },
                        "right_m": {
                            "type": "number",
                            "description": "Meters right (negative = left).",
                        },
                        "up_m": {
                            "type": "number",
                            "description": "Meters up (negative = down). 0 to hold altitude.",
                        },
                    },
                },
            }
        )
        tools.append(
            {
                "type": "function",
                "name": "body_goto",
                "description": (
                    "Fly/drive the body to absolute GPS coordinates. Only "
                    "use coordinates you got from body_status or the caller "
                    "— never invent them."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "latitude_deg": {"type": "number"},
                        "longitude_deg": {"type": "number"},
                        "altitude_m": {
                            "type": "number",
                            "description": (
                                "Target altitude in meters above the home "
                                "position. Omit to keep current altitude."
                            ),
                        },
                    },
                    "required": ["latitude_deg", "longitude_deg"],
                },
            }
        )

    if CAP_ROTATE in caps:
        tools.append(
            {
                "type": "function",
                "name": "body_turn",
                "description": (
                    "Rotate the body in place by a relative angle in degrees "
                    "(positive = clockwise/right, negative = "
                    "counter-clockwise/left)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "yaw_deg": {
                            "type": "number",
                            "description": "Relative turn in degrees, -180..180.",
                        }
                    },
                    "required": ["yaw_deg"],
                },
            }
        )

    if CAP_ALTITUDE in caps:
        tools.append(
            {
                "type": "function",
                "name": "body_takeoff",
                "description": (
                    "Arm and take off to a hover at the given altitude. Only "
                    "do this when the caller explicitly asks to take off or "
                    "fly."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "altitude_m": {
                            "type": "number",
                            "description": (
                                "Hover altitude in meters above ground "
                                "(default 2.5)."
                            ),
                        }
                    },
                },
            }
        )
        tools.append(
            {
                "type": "function",
                "name": "body_land",
                "description": "Land the body at its current position and disarm.",
                "parameters": {"type": "object", "properties": {}},
            }
        )

    return tools


def body_adapter_from_env() -> BodyAdapter | None:
    """Build a body adapter from ``GPTME_VOICE_BODY_URL``.

    - unset/empty  -> None (no body tools registered)
    - ``null``     -> NullAdapter (body_status only; tabletop puck)
    - ``mavsdk://<system_address>`` -> MavsdkAdapter, e.g.
      ``mavsdk://udpin://0.0.0.0:14540`` (SITL) or
      ``mavsdk://serial:///dev/ttyUSB0:57600`` (SiK radio)
    """
    url = os.environ.get("GPTME_VOICE_BODY_URL", "").strip()
    if not url:
        return None
    if url == "null":
        return NullAdapter()
    if url.startswith("mavsdk://"):
        from .mavsdk_adapter import MavsdkAdapter

        return MavsdkAdapter(url.removeprefix("mavsdk://"))
    logger.warning("Unknown GPTME_VOICE_BODY_URL scheme: %s (ignored)", url)
    return None
