"""Tests for the body adapter layer and its tool-bridge routing."""

import asyncio
import builtins
from typing import Any

import pytest
from gptme_voice.body import (
    NullAdapter,
    body_adapter_from_env,
    body_tool_schemas,
    conservative_mobile_characteristics,
    no_locomotion_characteristics,
)
from gptme_voice.body.adapter import (
    FALLBACK_MAX_ALTITUDE_M,
    FALLBACK_MAX_RADIUS_M,
    FALLBACK_MAX_SPEED_MPS,
    FALLBACK_RESERVE_RTL_PERCENT,
)
from gptme_voice.body.mavsdk_adapter import (
    body_to_ned,
    characteristics_from_px4_params,
    offset_latlon,
)
from gptme_voice.realtime.tool_bridge import GptmeToolBridge


class FakeAdapter:
    """Records calls; full capability set unless overridden."""

    name = "fake"
    requires_startup_connection = False

    def __init__(
        self,
        capabilities: set[str] | None = None,
        *,
        relative_altitude_m: float | None = None,
        in_air: bool = False,
    ):
        self.capabilities = (
            capabilities if capabilities is not None else {"move", "rotate", "altitude"}
        )
        self.calls: list[tuple[str, tuple]] = []
        self.connect_calls = 0
        self.relative_altitude_m = relative_altitude_m
        self.in_air = in_air

    async def ensure_connected(self) -> None:
        self.connect_calls += 1

    async def close(self) -> None:
        return None

    def telemetry(self) -> dict[str, Any]:
        position = (
            {"relative_altitude_m": self.relative_altitude_m}
            if self.relative_altitude_m is not None
            else None
        )
        return {"body": "fake", "in_air": self.in_air, "position": position}

    def characteristics(self) -> dict[str, Any]:
        if self.capabilities & {"move", "rotate", "altitude"}:
            return conservative_mobile_characteristics(source="declared")
        return no_locomotion_characteristics()

    async def takeoff(self, altitude_m: float) -> dict:
        self.calls.append(("takeoff", (altitude_m,)))
        return {"status": "taking_off"}

    async def land(self) -> dict:
        self.calls.append(("land", ()))
        return {"status": "landing"}

    async def goto(self, latitude_deg, longitude_deg, altitude_m) -> dict:
        self.calls.append(("goto", (latitude_deg, longitude_deg, altitude_m)))
        return {"status": "en_route"}

    async def move(self, forward_m, right_m, up_m) -> dict:
        self.calls.append(("move", (forward_m, right_m, up_m)))
        return {"status": "en_route"}

    async def turn(self, yaw_deg) -> dict:
        self.calls.append(("turn", (yaw_deg,)))
        return {"status": "turning"}

    async def stop(self) -> dict:
        self.calls.append(("stop", ()))
        return {"status": "holding"}

    async def return_home(self) -> dict:
        self.calls.append(("return_home", ()))
        return {"status": "returning_home"}


def _call(bridge: GptmeToolBridge, name: str, args: dict | None = None) -> dict:
    return asyncio.get_event_loop().run_until_complete(
        bridge.handle_function_call(name, args or {})
    )


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


# --- schema gating ------------------------------------------------------


def test_schemas_none_adapter_is_empty():
    assert body_tool_schemas(None) == []


def test_schemas_null_adapter_status_only():
    names = [t["name"] for t in body_tool_schemas(NullAdapter())]
    assert names == ["body_status"]


def test_schemas_full_capabilities():
    names = {t["name"] for t in body_tool_schemas(FakeAdapter())}
    assert names == {
        "body_status",
        "body_stop",
        "body_return_home",
        "body_move",
        "body_goto",
        "body_turn",
        "body_takeoff",
        "body_land",
    }


def test_schemas_move_only_excludes_altitude_tools():
    names = {t["name"] for t in body_tool_schemas(FakeAdapter({"move"}))}
    assert "body_takeoff" not in names
    assert "body_turn" not in names
    assert "body_move" in names
    assert "body_stop" in names


# --- bridge routing -----------------------------------------------------


def test_bridge_without_adapter_reports_no_body(loop):
    bridge = GptmeToolBridge()
    result = _call(bridge, "body_status")
    assert "error" in result
    assert "No body" in result["error"]


def test_bridge_status_and_stop(loop):
    adapter = FakeAdapter()
    bridge = GptmeToolBridge(body_adapter=adapter)
    status = _call(bridge, "body_status")
    assert status["status"] == "ok"
    assert status["telemetry"]["body"] == "fake"
    assert status["characteristics"]["locomotion"] is True
    assert status["characteristics"]["envelope"]["max_altitude_m"] > 0
    stop = _call(bridge, "body_stop")
    assert stop["status"] == "holding"
    assert ("stop", ()) in adapter.calls
    assert adapter.connect_calls == 2  # ensure_connected before every call


def test_bridge_refuses_takeoff_when_already_in_air(loop):
    adapter = FakeAdapter(in_air=True)
    bridge = GptmeToolBridge(body_adapter=adapter)
    result = _call(bridge, "body_takeoff", {"altitude_m": 2.5})
    assert "in the air" in result["error"]
    assert adapter.calls == []


def test_bridge_takeoff_clamps_altitude(loop, monkeypatch):
    monkeypatch.setenv("GPTME_VOICE_BODY_MAX_ALT_M", "30")
    adapter = FakeAdapter()
    bridge = GptmeToolBridge(body_adapter=adapter)
    _call(bridge, "body_takeoff", {"altitude_m": 500})
    assert adapter.calls[-1] == ("takeoff", (30.0,))
    _call(bridge, "body_takeoff", {"altitude_m": 0.1})
    assert adapter.calls[-1] == ("takeoff", (1.0,))
    _call(bridge, "body_takeoff", {})
    assert adapter.calls[-1] == ("takeoff", (2.5,))


@pytest.mark.parametrize("value", ["", "abc", "nan", "inf", "0", "-1"])
def test_bridge_invalid_body_limits_fall_back(loop, monkeypatch, value):
    monkeypatch.setenv("GPTME_VOICE_BODY_MAX_ALT_M", value)
    monkeypatch.setenv("GPTME_VOICE_BODY_MAX_MOVE_M", value)
    bridge = GptmeToolBridge(body_adapter=FakeAdapter())
    assert bridge.body_max_altitude_m == 30.0
    assert bridge.body_max_move_m == 50.0


def test_bridge_move_clamps_distance(loop):
    adapter = FakeAdapter()
    bridge = GptmeToolBridge(body_adapter=adapter)
    _call(bridge, "body_move", {"forward_m": 9999, "right_m": -9999, "up_m": 0})
    name, (f, r, u) = adapter.calls[-1]
    assert name == "move"
    assert f == bridge.body_max_move_m
    assert r == -bridge.body_max_move_m
    assert u == 0.0


def test_bridge_move_clamps_resulting_absolute_altitude(loop, monkeypatch):
    monkeypatch.setenv("GPTME_VOICE_BODY_MAX_ALT_M", "30")
    adapter = FakeAdapter(relative_altitude_m=25.0)
    bridge = GptmeToolBridge(body_adapter=adapter)

    _call(bridge, "body_move", {"up_m": 30})
    assert adapter.calls[-1] == ("move", (0.0, 0.0, 5.0))

    adapter.relative_altitude_m = 2.0
    _call(bridge, "body_move", {"up_m": -30})
    assert adapter.calls[-1] == ("move", (0.0, 0.0, -2.0))


def test_bridge_descent_on_ground_does_not_become_ascent(loop):
    adapter = FakeAdapter(relative_altitude_m=0.0)
    bridge = GptmeToolBridge(body_adapter=adapter)

    _call(bridge, "body_move", {"up_m": -1.0})

    assert adapter.calls[-1] == ("move", (0.0, 0.0, 0.0))


def test_bridge_move_refuses_climb_without_altitude_telemetry(loop):
    adapter = FakeAdapter()  # relative_altitude_m defaults to None
    bridge = GptmeToolBridge(body_adapter=adapter)

    result = _call(bridge, "body_move", {"up_m": 5.0})

    assert "error" in result
    assert "altitude unknown" in result["error"]
    assert adapter.calls == []


def test_bridge_move_allows_level_and_descent_without_altitude_telemetry(loop):
    adapter = FakeAdapter()
    bridge = GptmeToolBridge(body_adapter=adapter)

    _call(bridge, "body_move", {"forward_m": 2.0, "up_m": 0.0})
    assert adapter.calls[-1] == ("move", (2.0, 0.0, 0.0))

    _call(bridge, "body_move", {"up_m": -3.0})
    assert adapter.calls[-1] == ("move", (0.0, 0.0, -3.0))


def test_bridge_move_treats_non_object_position_as_missing_altitude(loop):
    class Adapter(FakeAdapter):
        def telemetry(self) -> dict[str, Any]:
            return {"position": "unknown"}

    adapter = Adapter()
    bridge = GptmeToolBridge(body_adapter=adapter)

    climb = _call(bridge, "body_move", {"up_m": 5.0})
    assert "error" in climb
    assert "altitude unknown" in climb["error"]
    assert adapter.calls == []

    _call(bridge, "body_move", {"forward_m": 1.0, "up_m": 0.0})
    assert adapter.calls[-1] == ("move", (1.0, 0.0, 0.0))


def test_bridge_capability_gate_blocks_uncapable_calls(loop):
    adapter = FakeAdapter({"move"})
    bridge = GptmeToolBridge(body_adapter=adapter)
    result = _call(bridge, "body_takeoff", {"altitude_m": 2})
    assert "error" in result
    assert adapter.calls == []


def test_bridge_goto_requires_coordinates(loop):
    adapter = FakeAdapter()
    bridge = GptmeToolBridge(body_adapter=adapter)
    result = _call(bridge, "body_goto", {"latitude_deg": 47.4})
    assert "error" in result


def test_bridge_body_error_is_reported_not_raised(loop):
    class ExplodingAdapter(FakeAdapter):
        async def stop(self) -> dict:
            raise RuntimeError("link down")

    bridge = GptmeToolBridge(body_adapter=ExplodingAdapter())
    result = _call(bridge, "body_stop")
    assert "body_stop failed" in result["error"]


def test_bridge_body_call_times_out(loop, monkeypatch):
    class HangingAdapter(FakeAdapter):
        async def stop(self) -> dict:
            await asyncio.Future()

    monkeypatch.setenv("GPTME_VOICE_BODY_CALL_TIMEOUT_S", "0.01")
    bridge = GptmeToolBridge(body_adapter=HangingAdapter())
    result = _call(bridge, "body_stop")
    assert "timed out" in result["error"]


def test_bridge_connect_times_out(loop, monkeypatch):
    class HangingAdapter(FakeAdapter):
        async def ensure_connected(self) -> None:
            await asyncio.Future()

    monkeypatch.setenv("GPTME_VOICE_BODY_CALL_TIMEOUT_S", "0.01")
    bridge = GptmeToolBridge(body_adapter=HangingAdapter())
    result = _call(bridge, "body_status")
    assert "timeout" in result["error"]


def test_bridge_turn_clamps_yaw(loop):
    adapter = FakeAdapter()
    bridge = GptmeToolBridge(body_adapter=adapter)
    _call(bridge, "body_turn", {"yaw_deg": 720})
    assert adapter.calls[-1] == ("turn", (180.0,))


# --- null adapter -------------------------------------------------------


def test_null_adapter_motion_reports_unsupported(loop):
    null = NullAdapter()
    result = asyncio.get_event_loop().run_until_complete(null.stop())
    assert "error" in result
    assert null.telemetry()["mobile"] is False
    characteristics = null.characteristics()
    assert characteristics["locomotion"] is False
    assert characteristics["envelope"] is None
    assert characteristics["geofence"] is None
    assert characteristics["endurance"] is None
    assert characteristics["link_loss"]["action"] == "none"
    assert characteristics["source"] == "declared"


# --- env factory --------------------------------------------------------


def test_adapter_from_env_unset(monkeypatch):
    monkeypatch.delenv("GPTME_VOICE_BODY_URL", raising=False)
    assert body_adapter_from_env() is None


def test_adapter_from_env_null(monkeypatch):
    monkeypatch.setenv("GPTME_VOICE_BODY_URL", "null")
    adapter = body_adapter_from_env()
    assert isinstance(adapter, NullAdapter)


def test_adapter_from_env_unknown_scheme(monkeypatch):
    monkeypatch.setenv("GPTME_VOICE_BODY_URL", "carrier-pigeon://coop")
    assert body_adapter_from_env() is None


def test_adapter_from_env_mavsdk(monkeypatch):
    pytest.importorskip("mavsdk")
    monkeypatch.setenv("GPTME_VOICE_BODY_URL", "mavsdk://udpin://0.0.0.0:14540")
    adapter = body_adapter_from_env()
    assert adapter is not None
    assert adapter.name == "mavsdk"
    assert adapter.system_address == "udpin://0.0.0.0:14540"


def test_adapter_from_env_mavsdk_missing_dependency(monkeypatch):
    monkeypatch.setenv("GPTME_VOICE_BODY_URL", "mavsdk://udpin://0.0.0.0:14540")
    original_import = builtins.__import__

    def import_without_mavsdk(name, *args, **kwargs):
        if name == "mavsdk":
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_mavsdk)
    assert body_adapter_from_env() is None


# --- mavsdk geometry helpers (pure math, no mavsdk import needed) -------


def test_body_to_ned_facing_north():
    north, east = body_to_ned(10, 0, 0)
    assert north == pytest.approx(10)
    assert east == pytest.approx(0)


def test_body_to_ned_facing_east():
    north, east = body_to_ned(10, 0, 90)
    assert north == pytest.approx(0, abs=1e-9)
    assert east == pytest.approx(10)


def test_body_to_ned_right_component():
    # Facing north, moving right 5 m => 5 m east
    north, east = body_to_ned(0, 5, 0)
    assert north == pytest.approx(0)
    assert east == pytest.approx(5)


def test_offset_latlon_north():
    lat, lon = offset_latlon(47.0, 8.0, 111_320.0, 0.0)
    assert lat == pytest.approx(48.0)
    assert lon == pytest.approx(8.0)


def test_offset_latlon_east_scales_with_latitude():
    _, lon_equator = offset_latlon(0.0, 8.0, 0.0, 1000.0)
    _, lon_north = offset_latlon(60.0, 8.0, 0.0, 1000.0)
    # Same eastward meters => larger longitude delta at higher latitude
    assert (lon_north - 8.0) > (lon_equator - 8.0)


# --- MAVSDK safety behavior --------------------------------------------


def test_mavsdk_move_never_targets_below_home(loop):
    from gptme_voice.body.mavsdk_adapter import MavsdkAdapter

    class Action:
        def __init__(self):
            self.goto_args = None

        async def goto_location(self, *args):
            self.goto_args = args

    action = Action()
    adapter = MavsdkAdapter("unused")
    adapter._system = type("System", (), {"action": action})()
    adapter._position = {
        "latitude_deg": 47.0,
        "longitude_deg": 8.0,
        "absolute_altitude_m": 502.0,
        "relative_altitude_m": 2.0,
    }
    adapter._heading_deg = 0.0

    result = loop.run_until_complete(adapter.move(0.0, 0.0, -30.0))

    assert result["target"]["altitude_m"] == 0.0
    assert action.goto_args[2] == 500.0


def test_mavsdk_move_and_turn_require_heading(loop):
    from gptme_voice.body.mavsdk_adapter import MavsdkAdapter

    adapter = MavsdkAdapter("unused")
    adapter._system = object()
    adapter._position = {
        "latitude_deg": 47.0,
        "longitude_deg": 8.0,
        "absolute_altitude_m": 502.0,
        "relative_altitude_m": 2.0,
    }

    move = loop.run_until_complete(adapter.move(1.0, 0.0, 0.0))
    turn = loop.run_until_complete(adapter.turn(90.0))
    goto = loop.run_until_complete(adapter.goto(47.1, 8.1, 10.0))

    assert "heading fix" in move["error"]
    assert "heading fix" in turn["error"]
    assert "heading fix" in goto["error"]


def test_mavsdk_takeoff_refuses_when_in_air(loop):
    from gptme_voice.body.mavsdk_adapter import MavsdkAdapter

    class Action:
        async def set_takeoff_altitude(self, _altitude: float) -> None:
            raise AssertionError("takeoff must not run while in air")

        async def arm(self) -> None:
            raise AssertionError("takeoff must not run while in air")

        async def takeoff(self) -> None:
            raise AssertionError("takeoff must not run while in air")

    adapter = MavsdkAdapter("unused")
    adapter._system = type("System", (), {"action": Action()})()
    adapter._in_air = True
    result = loop.run_until_complete(adapter.takeoff(2.5))
    assert "in the air" in result["error"]


def test_mavsdk_action_timeout(loop):
    from gptme_voice.body.mavsdk_adapter import MavsdkAdapter

    class Action:
        async def hold(self):
            await asyncio.Future()

    adapter = MavsdkAdapter("unused", command_timeout_s=0.01)
    adapter._system = type("System", (), {"action": Action()})()
    adapter._connected = True

    with pytest.raises(TimeoutError):
        loop.run_until_complete(adapter.stop())

    # Timeout must not drop the link: the vehicle may still be executing.
    assert adapter._connected is True


def test_mavsdk_close_releases_system(loop):
    from gptme_voice.body.mavsdk_adapter import MavsdkAdapter

    class System:
        stopped = False

        def _stop_mavsdk_server(self):
            self.stopped = True

    system = System()
    adapter = MavsdkAdapter("unused")
    adapter._system = system

    loop.run_until_complete(adapter.close())

    assert system.stopped is True
    assert adapter._system is None


def test_mavsdk_serializes_action_commands(loop):
    from gptme_voice.body.mavsdk_adapter import MavsdkAdapter

    class Action:
        def __init__(self):
            self.active = 0
            self.max_active = 0

        async def hold(self):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0)
            self.active -= 1

    async def run() -> int:
        action = Action()
        adapter = MavsdkAdapter("unused")
        adapter._system = type("System", (), {"action": action})()
        await asyncio.gather(adapter.stop(), adapter.stop())
        return action.max_active

    assert loop.run_until_complete(run()) == 1


def test_mavsdk_telemetry_failure_marks_disconnected(loop):
    from gptme_voice.body.mavsdk_adapter import MavsdkAdapter

    async def broken_stream():
        if False:
            yield None
        raise ConnectionError("link lost")

    async def waiting_stream():
        while True:
            await asyncio.sleep(10)
            yield None

    class Telemetry:
        position = staticmethod(broken_stream)
        heading = staticmethod(waiting_stream)
        battery = staticmethod(waiting_stream)
        in_air = staticmethod(waiting_stream)
        flight_mode = staticmethod(waiting_stream)
        armed = staticmethod(waiting_stream)

    async def run() -> tuple[bool, dict[str, Any], list[asyncio.Task], dict[str, Any]]:
        adapter = MavsdkAdapter("unused")
        adapter._system = type("System", (), {"telemetry": Telemetry()})()
        adapter._connected = True
        adapter._position = {"relative_altitude_m": 2.0}
        adapter._start_telemetry_cache()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return (
            adapter._connected,
            adapter._position,
            adapter._telemetry_tasks,
            adapter.characteristics(),
        )

    connected, position, tasks, characteristics = loop.run_until_complete(run())
    assert connected is False
    assert position == {}
    assert tasks == []
    assert characteristics["source"] == "fallback"


def _assert_finite_envelope(payload: dict[str, Any]) -> None:
    envelope = payload["envelope"]
    assert envelope is not None
    for key in ("max_altitude_m", "max_radius_m", "max_horizontal_speed_mps"):
        value = envelope[key]
        assert isinstance(value, int | float) and not isinstance(value, bool)
        assert value > 0
        assert value != float("inf")
    assert payload["link_loss"]["action"] != "none"


def test_px4_params_populate_envelope():
    payload = characteristics_from_px4_params(
        {
            "GF_MAX_HOR_DIST": 80.0,
            "GF_MAX_VER_DIST": 25.0,
            "MPC_XY_VEL_MAX": 8.0,
            "NAV_DLL_ACT": 2,
            "NAV_RCL_ACT": 1,
            "COM_DL_LOSS_T": 12,
            "BAT_LOW_THR": 0.2,
        },
        battery_remaining_percent=67.0,
    )
    _assert_finite_envelope(payload)
    assert payload["source"] == "px4-params"
    assert payload["locomotion"] is True
    assert payload["envelope"]["max_radius_m"] == 80.0
    assert payload["envelope"]["max_altitude_m"] == 25.0
    assert payload["envelope"]["max_horizontal_speed_mps"] == 8.0
    assert payload["geofence"]["enabled"] is True
    assert payload["link_loss"]["action"] == "rtl"
    assert payload["link_loss"]["timeout_s"] == 12.0
    assert payload["link_loss"]["authority"] == "px4"
    assert payload["endurance"]["reserve_rtl_percent"] == pytest.approx(20.0)
    assert payload["endurance"]["battery_remaining_percent"] == 67.0
    assert payload["endurance"]["remaining_s"] is None


def test_px4_zero_geofence_is_not_unlimited():
    payload = characteristics_from_px4_params(
        {
            "GF_MAX_HOR_DIST": 0,
            "GF_MAX_VER_DIST": 0,
            "MPC_XY_VEL_MAX": 12.0,
            "NAV_DLL_ACT": 0,
            "NAV_RCL_ACT": 0,
        }
    )
    _assert_finite_envelope(payload)
    assert payload["source"] == "mixed"
    assert payload["envelope"]["max_radius_m"] == FALLBACK_MAX_RADIUS_M
    assert payload["envelope"]["max_altitude_m"] == FALLBACK_MAX_ALTITUDE_M
    assert payload["envelope"]["max_horizontal_speed_mps"] == 12.0
    assert payload["geofence"]["enabled"] is False
    assert payload["link_loss"]["action"] == "rtl"


def test_px4_missing_params_use_conservative_fallback():
    payload = characteristics_from_px4_params({})
    _assert_finite_envelope(payload)
    assert payload["source"] == "fallback"
    assert payload["envelope"]["max_altitude_m"] == FALLBACK_MAX_ALTITUDE_M
    assert payload["envelope"]["max_radius_m"] == FALLBACK_MAX_RADIUS_M
    assert payload["envelope"]["max_horizontal_speed_mps"] == FALLBACK_MAX_SPEED_MPS
    assert payload["endurance"]["reserve_rtl_percent"] == FALLBACK_RESERVE_RTL_PERCENT
    assert payload["link_loss"]["action"] == "rtl"


def test_mavsdk_characteristics_from_overrides_and_live_battery(loop):
    from gptme_voice.body.mavsdk_adapter import MavsdkAdapter

    adapter = MavsdkAdapter("unused")
    adapter._param_overrides = {
        "GF_MAX_HOR_DIST": 40.0,
        "GF_MAX_VER_DIST": 15.0,
        "MPC_XY_VEL_MAX": 6.0,
        "NAV_DLL_ACT": 3,
        "NAV_RCL_ACT": 2,
        "COM_DL_LOSS_T": 8,
        "BAT_LOW_THR": 0.3,
    }
    loop.run_until_complete(adapter._load_characteristics())
    first = adapter.characteristics()
    assert first["envelope"]["max_radius_m"] == 40.0
    assert first["link_loss"]["action"] == "land"
    assert first["endurance"]["battery_remaining_percent"] is None

    adapter._battery = {"remaining_percent": 41.5}
    assert adapter.characteristics()["endurance"]["battery_remaining_percent"] == 41.5


def test_bridge_null_adapter_surfaces_no_locomotion_characteristics(loop):
    bridge = GptmeToolBridge(body_adapter=NullAdapter())
    status = _call(bridge, "body_status")
    assert status["status"] == "ok"
    assert status["characteristics"]["locomotion"] is False
    assert status["characteristics"]["envelope"] is None
