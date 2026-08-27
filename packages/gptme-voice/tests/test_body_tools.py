"""Tests for the body adapter layer and its tool-bridge routing."""

import asyncio
from typing import Any

import pytest
from gptme_voice.body import (
    NullAdapter,
    body_adapter_from_env,
    body_tool_schemas,
)
from gptme_voice.body.mavsdk_adapter import body_to_ned, offset_latlon
from gptme_voice.realtime.tool_bridge import GptmeToolBridge


class FakeAdapter:
    """Records calls; full capability set unless overridden."""

    name = "fake"

    def __init__(self, capabilities: set[str] | None = None):
        self.capabilities = (
            capabilities if capabilities is not None else {"move", "rotate", "altitude"}
        )
        self.calls: list[tuple[str, tuple]] = []
        self.connect_calls = 0

    async def ensure_connected(self) -> None:
        self.connect_calls += 1

    def telemetry(self) -> dict[str, Any]:
        return {"body": "fake", "in_air": False}

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
    stop = _call(bridge, "body_stop")
    assert stop["status"] == "holding"
    assert ("stop", ()) in adapter.calls
    assert adapter.connect_calls == 2  # ensure_connected before every call


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


def test_bridge_move_clamps_distance(loop):
    adapter = FakeAdapter()
    bridge = GptmeToolBridge(body_adapter=adapter)
    _call(bridge, "body_move", {"forward_m": 9999, "right_m": -9999, "up_m": 0})
    name, (f, r, u) = adapter.calls[-1]
    assert name == "move"
    assert f == bridge.body_max_move_m
    assert r == -bridge.body_max_move_m
    assert u == 0.0


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
