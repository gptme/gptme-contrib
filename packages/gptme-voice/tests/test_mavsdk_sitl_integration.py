"""Live SITL integration test for MavsdkAdapter.

Skipped unless BODY_SITL_URL is set (e.g. udpin://0.0.0.0:14540 with a
running PX4 SITL such as jonasvautherin/px4-gazebo-headless). Exercises
the full voice-tool path: bridge -> adapter -> simulated PX4 vehicle.

    docker run -d -t --name px4-sitl jonasvautherin/px4-gazebo-headless:1.15.0
    BODY_SITL_URL=udpin://0.0.0.0:14540 uv run pytest \
        packages/gptme-voice/tests/test_mavsdk_sitl_integration.py -v
"""

import asyncio
import os

import pytest

SITL_URL = os.environ.get("BODY_SITL_URL")

pytestmark = pytest.mark.skipif(
    not SITL_URL, reason="BODY_SITL_URL not set (needs a running PX4 SITL)"
)


def test_full_flight_via_bridge():
    pytest.importorskip("mavsdk")
    from gptme_voice.body.mavsdk_adapter import MavsdkAdapter
    from gptme_voice.realtime.tool_bridge import GptmeToolBridge

    async def scenario() -> None:
        adapter = MavsdkAdapter(SITL_URL)
        bridge = GptmeToolBridge(body_adapter=adapter)

        async def call(name: str, args: dict | None = None) -> dict:
            return await bridge.handle_function_call(name, args or {})

        # status: wait for a position fix
        for _ in range(60):
            status = await call("body_status")
            assert status["status"] == "ok"
            if status["telemetry"]["position"]:
                break
            await asyncio.sleep(1)
        assert status["telemetry"]["position"], "no position fix from SITL"

        # takeoff
        result = await call("body_takeoff", {"altitude_m": 2.5})
        assert result["status"] == "taking_off"
        for _ in range(60):
            await asyncio.sleep(1)
            telemetry = (await call("body_status"))["telemetry"]
            if (telemetry["position"] or {}).get("relative_altitude_m", 0) > 2.0:
                break
        assert telemetry["position"]["relative_altitude_m"] > 2.0

        # relative move: forward 5 m
        result = await call("body_move", {"forward_m": 5.0})
        assert result["status"] == "en_route"
        await asyncio.sleep(5)

        # stop (hold)
        result = await call("body_stop")
        assert result["status"] == "holding"

        # land
        result = await call("body_land")
        assert result["status"] == "landing"
        for _ in range(90):
            await asyncio.sleep(1)
            telemetry = (await call("body_status"))["telemetry"]
            if telemetry["in_air"] is False:
                break
        assert telemetry["in_air"] is False

        await adapter.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=180))
