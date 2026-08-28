"""MAVSDK body adapter — PX4 vehicles (X500 v2 quad, SITL).

Translates BodyAdapter goals into MAVSDK action calls. Deliberately uses
only the high-level Action API (goto_location, takeoff, land, hold, RTL) —
no offboard control loops. PX4's own failsafes remain the safety authority:
if the brain link drops mid-goal, the vehicle keeps its autopilot behavior.

Validated against PX4 SITL (jonasvautherin/px4-gazebo-headless);
see projects/bobbody-flight/ in the Bob workspace for the harness.

Requires the optional ``mavsdk`` dependency: ``gptme-voice[body]``.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

_EARTH_M_PER_DEG_LAT = 111_320.0


def body_to_ned(
    forward_m: float, right_m: float, yaw_deg: float
) -> tuple[float, float]:
    """Rotate a body-frame (forward, right) offset into NED (north, east).

    ``yaw_deg`` is heading from north, clockwise-positive (NED convention).
    """
    yaw = math.radians(yaw_deg)
    north = forward_m * math.cos(yaw) - right_m * math.sin(yaw)
    east = forward_m * math.sin(yaw) + right_m * math.cos(yaw)
    return north, east


def offset_latlon(
    latitude_deg: float, longitude_deg: float, north_m: float, east_m: float
) -> tuple[float, float]:
    """Offset a lat/lon by meters north/east (small-distance approximation)."""
    dlat = north_m / _EARTH_M_PER_DEG_LAT
    denom = _EARTH_M_PER_DEG_LAT * math.cos(math.radians(latitude_deg))
    dlon = east_m / denom if abs(denom) > 1e-9 else 0.0
    return latitude_deg + dlat, longitude_deg + dlon


class MavsdkAdapter:
    """BodyAdapter for PX4/MAVSDK vehicles."""

    capabilities = {"move", "rotate", "altitude"}
    name = "mavsdk"

    def __init__(
        self,
        system_address: str,
        connect_timeout_s: float = 5.0,
        command_timeout_s: float = 10.0,
    ):
        self.system_address = system_address
        self.connect_timeout_s = connect_timeout_s
        self.command_timeout_s = command_timeout_s
        self._system: Any = None
        self._connected = False
        # Command calls are serialized, and reconnect takes this lock after the
        # connection lock so a new System cannot replace one in active use.
        self._connection_lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()
        self._telemetry_tasks: list[asyncio.Task] = []
        # Telemetry caches (filled by background subscriptions)
        self._position: dict[str, float] = {}
        self._heading_deg: float | None = None
        self._battery: dict[str, float] = {}
        self._in_air: bool | None = None
        self._flight_mode: str | None = None
        self._armed: bool | None = None

    async def ensure_connected(self) -> None:
        async with self._connection_lock:
            if self._connected:
                return
            async with self._command_lock:
                if self._connected:
                    return
                await self._cancel_telemetry_tasks()
                await self._release_system()
                from mavsdk import System  # type: ignore[import-not-found]

                system = System()
                logger.info("MavsdkAdapter connecting to %s", self.system_address)
                try:
                    await asyncio.wait_for(
                        system.connect(system_address=self.system_address),
                        timeout=self.connect_timeout_s,
                    )

                    async def _wait_connected() -> None:
                        async for state in system.core.connection_state():
                            if state.is_connected:
                                return

                    await asyncio.wait_for(
                        _wait_connected(), timeout=self.connect_timeout_s
                    )
                except BaseException:
                    self._stop_system(system)
                    raise
                self._system = system
                self._start_telemetry_cache()
                self._connected = True
                logger.info("MavsdkAdapter connected")

    def _start_telemetry_cache(self) -> None:
        system = self._system

        async def _position() -> None:
            async for p in system.telemetry.position():
                self._position = {
                    "latitude_deg": p.latitude_deg,
                    "longitude_deg": p.longitude_deg,
                    "absolute_altitude_m": p.absolute_altitude_m,
                    "relative_altitude_m": p.relative_altitude_m,
                }

        async def _heading() -> None:
            async for h in system.telemetry.heading():
                self._heading_deg = h.heading_deg

        async def _battery() -> None:
            async for b in system.telemetry.battery():
                self._battery = {
                    "voltage_v": round(b.voltage_v, 2),
                    "remaining_percent": round(b.remaining_percent, 1),
                }

        async def _in_air() -> None:
            async for in_air in system.telemetry.in_air():
                self._in_air = in_air

        async def _flight_mode() -> None:
            async for mode in system.telemetry.flight_mode():
                self._flight_mode = str(mode)

        async def _armed() -> None:
            async for armed in system.telemetry.armed():
                self._armed = armed

        async def _watch(stream_name: str, subscription_factory: Any) -> None:
            try:
                await subscription_factory()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - MAVSDK stream errors vary by backend
                logger.exception("MAVSDK %s telemetry stream failed", stream_name)
                self._mark_disconnected()
            else:
                logger.error("MAVSDK %s telemetry stream ended", stream_name)
                self._mark_disconnected()

        subscriptions = {
            "position": _position,
            "heading": _heading,
            "battery": _battery,
            "in_air": _in_air,
            "flight_mode": _flight_mode,
            "armed": _armed,
        }
        self._telemetry_tasks.extend(
            asyncio.create_task(_watch(name, subscription_factory))
            for name, subscription_factory in subscriptions.items()
        )

    def _mark_disconnected(self) -> None:
        self._connected = False
        self._position = {}
        self._heading_deg = None
        self._battery = {}
        self._in_air = None
        self._flight_mode = None
        self._armed = None
        current = asyncio.current_task()
        for task in self._telemetry_tasks:
            if task is not current:
                task.cancel()
        self._telemetry_tasks.clear()

    async def _cancel_telemetry_tasks(self) -> None:
        tasks = list(self._telemetry_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._telemetry_tasks.clear()

    @staticmethod
    def _stop_system(system: Any) -> None:
        """Release MAVSDK-Python's subprocess and gRPC resources.

        MAVSDK-Python 2.x has no public close method. Its destructor invokes
        this cleanup hook, but garbage collection is too late for reconnects.
        """
        stop = getattr(system, "_stop_mavsdk_server", None)
        if callable(stop):
            stop()

    async def _release_system(self) -> None:
        system, self._system = self._system, None
        if system is not None:
            self._stop_system(system)

    async def close(self) -> None:
        async with self._connection_lock:
            async with self._command_lock:
                await self._cancel_telemetry_tasks()
                self._mark_disconnected()
                await self._release_system()

    async def _run_action(self, awaitable: Any) -> Any:
        return await asyncio.wait_for(awaitable, timeout=self.command_timeout_s)

    # -- BodyAdapter interface -------------------------------------------

    def telemetry(self) -> dict[str, Any]:
        return {
            "body": self.name,
            "mobile": True,
            "connected": self._connected,
            "position": self._position or None,
            "heading_deg": self._heading_deg,
            "battery": self._battery or None,
            "in_air": self._in_air,
            "flight_mode": self._flight_mode,
            "armed": self._armed,
        }

    async def takeoff(self, altitude_m: float) -> dict[str, Any]:
        async with self._command_lock:
            system = self._system
            await self._run_action(system.action.set_takeoff_altitude(altitude_m))
            await self._run_action(system.action.arm())
            await self._run_action(system.action.takeoff())
            return {
                "status": "taking_off",
                "target_altitude_m": altitude_m,
                "message": "Armed and taking off. Check body_status for altitude.",
            }

    async def land(self) -> dict[str, Any]:
        async with self._command_lock:
            await self._run_action(self._system.action.land())
            return {"status": "landing"}

    async def stop(self) -> dict[str, Any]:
        async with self._command_lock:
            await self._run_action(self._system.action.hold())
            return {
                "status": "holding",
                "message": "Motion stopped; holding position.",
            }

    async def return_home(self) -> dict[str, Any]:
        async with self._command_lock:
            await self._run_action(self._system.action.return_to_launch())
            return {"status": "returning_home"}

    async def goto(
        self,
        latitude_deg: float,
        longitude_deg: float,
        altitude_m: float | None,
    ) -> dict[str, Any]:
        async with self._command_lock:
            return await self._goto_unlocked(latitude_deg, longitude_deg, altitude_m)

    async def _goto_unlocked(
        self,
        latitude_deg: float,
        longitude_deg: float,
        altitude_m: float | None,
    ) -> dict[str, Any]:
        pos = self._position
        if not pos:
            return {"error": "No position fix yet; cannot goto."}
        home_abs = pos["absolute_altitude_m"] - pos["relative_altitude_m"]
        target_abs = (
            home_abs + altitude_m
            if altitude_m is not None
            else pos["absolute_altitude_m"]
        )
        yaw = self._heading_deg if self._heading_deg is not None else float("nan")
        await self._run_action(
            self._system.action.goto_location(
                latitude_deg, longitude_deg, target_abs, yaw
            )
        )
        return {
            "status": "en_route",
            "target": {
                "latitude_deg": latitude_deg,
                "longitude_deg": longitude_deg,
                "altitude_m": altitude_m,
            },
        }

    async def move(
        self, forward_m: float, right_m: float, up_m: float
    ) -> dict[str, Any]:
        async with self._command_lock:
            pos = self._position
            if not pos:
                return {"error": "No position fix yet; cannot move."}
            if self._heading_deg is None:
                return {"error": "No heading fix yet; cannot move safely."}
            yaw = self._heading_deg
            north, east = body_to_ned(forward_m, right_m, yaw)
            lat, lon = offset_latlon(
                pos["latitude_deg"], pos["longitude_deg"], north, east
            )
            target_rel = max(1.0, pos["relative_altitude_m"] + up_m)
            return await self._goto_unlocked(lat, lon, target_rel)

    async def turn(self, yaw_deg: float) -> dict[str, Any]:
        async with self._command_lock:
            pos = self._position
            if not pos:
                return {"error": "No position fix yet; cannot turn."}
            if self._heading_deg is None:
                return {"error": "No heading fix yet; cannot turn safely."}
            target_yaw = (self._heading_deg + yaw_deg + 180.0) % 360.0 - 180.0
            await self._run_action(
                self._system.action.goto_location(
                    pos["latitude_deg"],
                    pos["longitude_deg"],
                    pos["absolute_altitude_m"],
                    target_yaw,
                )
            )
            return {
                "status": "turning",
                "target_heading_deg": round(target_yaw, 1),
            }
