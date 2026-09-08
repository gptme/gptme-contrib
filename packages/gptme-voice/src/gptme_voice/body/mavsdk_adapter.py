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
from collections.abc import Mapping
from typing import Any

from .adapter import (
    FALLBACK_LINK_LOSS_TIMEOUT_S,
    FALLBACK_MAX_ALTITUDE_M,
    FALLBACK_MAX_RADIUS_M,
    FALLBACK_MAX_SPEED_MPS,
    FALLBACK_RESERVE_RTL_PERCENT,
    FlightEnvelope,
    Geofence,
    LinkLossAction,
    conservative_mobile_characteristics,
)

logger = logging.getLogger(__name__)

_EARTH_M_PER_DEG_LAT = 111_320.0

# PX4 params that populate the characteristics descriptor. A geofence
# distance of 0 means "disabled" (unlimited) — never advertise that.
_PX4_PARAM_NAMES = (
    "GF_MAX_HOR_DIST",
    "GF_MAX_VER_DIST",
    "MPC_XY_VEL_MAX",
    "NAV_RCL_ACT",
    "NAV_DLL_ACT",
    "COM_RC_LOSS_T",
    "COM_DL_LOSS_T",
    "BAT_LOW_THR",
)

_PX4_FAILSAFE_ACTION: dict[int, LinkLossAction] = {
    0: "none",
    1: "hold",
    2: "rtl",
    3: "land",
    4: "terminate",
    5: "disarm",
}


def _finite_positive(value: object) -> float | None:
    """Return a finite number > 0, else None.

    PX4 uses 0 on several limit params to mean 'disabled' / unlimited.
    Those must degrade to the conservative fallback, never pass through.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _px4_failsafe_action(value: object) -> LinkLossAction | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if float(value) != int(value):
        return None
    return _PX4_FAILSAFE_ACTION.get(int(value))


def characteristics_from_px4_params(
    params: Mapping[str, float | int],
    *,
    battery_remaining_percent: float | None = None,
) -> dict[str, Any]:
    """Build a characteristics dict from PX4 params.

    Missing, non-finite, or non-positive limit params fall back to the
    conservative static envelope. ``source`` is ``px4-params`` when every
    limit we care about was present, else ``mixed`` or ``fallback``.
    """
    gaps: list[str] = []

    radius_raw = _finite_positive(params.get("GF_MAX_HOR_DIST"))
    altitude_raw = _finite_positive(params.get("GF_MAX_VER_DIST"))
    if radius_raw is None:
        gaps.append("GF_MAX_HOR_DIST")
    if altitude_raw is None:
        gaps.append("GF_MAX_VER_DIST")
    radius = radius_raw if radius_raw is not None else FALLBACK_MAX_RADIUS_M
    altitude = altitude_raw if altitude_raw is not None else FALLBACK_MAX_ALTITUDE_M
    fence_enabled = radius_raw is not None or altitude_raw is not None

    speed = _finite_positive(params.get("MPC_XY_VEL_MAX"))
    if speed is None:
        gaps.append("MPC_XY_VEL_MAX")
        speed = FALLBACK_MAX_SPEED_MPS

    # Brain-link loss is closer to datalink loss than RC loss. PX4 action 0
    # means "disabled"; a flying body must not advertise "none".
    dll = _px4_failsafe_action(params.get("NAV_DLL_ACT"))
    rcl = _px4_failsafe_action(params.get("NAV_RCL_ACT"))
    if dll is None:
        gaps.append("NAV_DLL_ACT")
    if rcl is None:
        gaps.append("NAV_RCL_ACT")
    action: LinkLossAction = "rtl"
    for candidate in (dll, rcl):
        if candidate is not None and candidate != "none":
            action = candidate
            break

    timeout = _finite_positive(params.get("COM_DL_LOSS_T"))
    if timeout is None:
        timeout = _finite_positive(params.get("COM_RC_LOSS_T"))
    if timeout is None:
        gaps.append("COM_DL_LOSS_T")
        timeout = FALLBACK_LINK_LOSS_TIMEOUT_S

    reserve = _finite_positive(params.get("BAT_LOW_THR"))
    if reserve is None:
        gaps.append("BAT_LOW_THR")
        reserve_percent = FALLBACK_RESERVE_RTL_PERCENT
    else:
        # BAT_LOW_THR is 0-1; the descriptor advertises percent.
        reserve_percent = reserve * 100.0 if reserve <= 1.0 else reserve

    if not gaps:
        source = "px4-params"
        notes = "Populated from live PX4 parameters. Autopilot failsafes remain final."
    elif len(gaps) >= 5:
        source = "fallback"
        notes = (
            "PX4 param read missed the envelope; using conservative static "
            "limits. Autopilot failsafes remain final."
        )
    else:
        source = "mixed"
        notes = (
            "Some PX4 params were missing or disabled (0 = unlimited); "
            f"fell back for: {', '.join(gaps)}. Autopilot failsafes remain final."
        )

    return conservative_mobile_characteristics(
        source=source,
        notes=notes,
        authority="px4",
        link_loss_action=action,
        link_loss_timeout_s=timeout,
        reserve_rtl_percent=reserve_percent,
        battery_remaining_percent=battery_remaining_percent,
        envelope=FlightEnvelope(
            max_altitude_m=altitude,
            max_radius_m=radius,
            max_horizontal_speed_mps=speed,
        ),
        geofence=Geofence(
            enabled=fence_enabled,
            max_radius_m=radius,
            max_altitude_m=altitude,
        ),
    )


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
    requires_startup_connection = False

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
        # Characteristics cache. Tests can set _param_overrides to skip MAVSDK I/O.
        self._param_overrides: dict[str, float | int] | None = None
        self._characteristics: dict[str, Any] = conservative_mobile_characteristics()

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
                await self._load_characteristics()
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
        self._characteristics = conservative_mobile_characteristics()
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
        try:
            return await asyncio.wait_for(awaitable, timeout=self.command_timeout_s)
        except asyncio.TimeoutError:
            # The MAVSDK command is already on the vehicle. Dropping the
            # adapter here would cancel telemetry and invite a reconnect
            # while land/takeoff/goto is still executing.
            # Python 3.10: wait_for raises asyncio.TimeoutError, distinct from
            # builtin TimeoutError. Re-raise TimeoutError so callers and tests
            # can catch one type on every supported Python.
            logger.warning(
                "MAVSDK action timed out after %.1fs; leaving adapter connected",
                self.command_timeout_s,
            )
            raise TimeoutError from None

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

    def characteristics(self) -> dict[str, Any]:
        payload = dict(self._characteristics)
        endurance = payload.get("endurance")
        if isinstance(endurance, dict):
            endurance = dict(endurance)
            remaining = self._battery.get("remaining_percent")
            if remaining is not None:
                endurance["battery_remaining_percent"] = remaining
            payload["endurance"] = endurance
        return payload

    async def _load_characteristics(self) -> None:
        if self._param_overrides is not None:
            raw: dict[str, float | int] = dict(self._param_overrides)
        else:
            raw = await self._read_px4_params()
        self._characteristics = characteristics_from_px4_params(
            raw,
            battery_remaining_percent=self._battery.get("remaining_percent"),
        )

    async def _read_px4_params(self) -> dict[str, float | int]:
        system = self._system
        if system is None:
            return {}
        collected = await self._read_all_px4_params(system)
        if collected is not None:
            return {
                name: collected[name] for name in _PX4_PARAM_NAMES if name in collected
            }
        raw: dict[str, float | int] = {}
        for name in _PX4_PARAM_NAMES:
            value = await self._get_param(system, name)
            if value is not None:
                raw[name] = value
        return raw

    @staticmethod
    async def _read_all_px4_params(system: Any) -> dict[str, float | int] | None:
        getter = getattr(getattr(system, "param", None), "get_all_params", None)
        if not callable(getter):
            return None
        try:
            all_params = await asyncio.wait_for(getter(), timeout=5.0)
        except Exception:
            logger.debug(
                "MAVSDK get_all_params failed; falling back to per-name reads",
                exc_info=True,
            )
            return None
        collected: dict[str, float | int] = {}
        for group in (
            getattr(all_params, "int_params", None),
            getattr(all_params, "float_params", None),
        ):
            if not group:
                continue
            for param in group:
                name = getattr(param, "name", None)
                value = getattr(param, "value", None)
                if (
                    isinstance(name, str)
                    and isinstance(value, int | float)
                    and not isinstance(value, bool)
                ):
                    collected[name] = value
        return collected

    @staticmethod
    async def _get_param(system: Any, name: str) -> float | int | None:
        param = getattr(system, "param", None)
        if param is None:
            return None
        for getter_name in ("get_param_float", "get_param_int"):
            getter = getattr(param, getter_name, None)
            if not callable(getter):
                continue
            try:
                value = await asyncio.wait_for(getter(name), timeout=1.0)
            except Exception:
                continue
            if isinstance(value, int | float) and not isinstance(value, bool):
                return value
        return None

    async def takeoff(self, altitude_m: float) -> dict[str, Any]:
        async with self._command_lock:
            if self._in_air is True:
                return {
                    "error": (
                        "Already in the air; refuse takeoff. "
                        "Use body_move, body_goto, or body_land."
                    )
                }
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

    async def interact(self) -> dict[str, Any]:
        return {"error": "MAVSDK bodies have no generic interaction capability."}

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
        if self._heading_deg is None:
            return {"error": "No heading fix yet; cannot goto safely."}
        home_abs = pos["absolute_altitude_m"] - pos["relative_altitude_m"]
        target_abs = (
            home_abs + altitude_m
            if altitude_m is not None
            else pos["absolute_altitude_m"]
        )
        yaw = self._heading_deg
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
            target_rel = max(0.0, pos["relative_altitude_m"] + up_m)
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
