"""Tests for the remote body-node adapter and neutral wire protocol."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from gptme_voice.body import RemoteAdapter, body_adapter_from_env, body_tool_schemas
from gptme_voice.body.remote_adapter import is_loopback_host
from gptme_voice.realtime.tool_bridge import GptmeToolBridge

_HANDSHAKE_OK = {
    "type": "handshake_ok",
    "protocol": "bob-body/0",
    "body_id": "bob-world-local",
    "capabilities": ["status", "move", "turn", "stop", "interact"],
    "telemetry": {"body_id": "bob-world-local"},
}


async def _body_node(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    requests: list[dict[str, Any]],
) -> None:
    while line := await reader.readline():
        request = json.loads(line)
        requests.append(request)
        if request["type"] == "handshake":
            response = dict(_HANDSHAKE_OK)
        else:
            response = {
                "type": "command_result",
                "command_id": request["command_id"],
                "status": (
                    "accepted"
                    if request["command"] in {"move", "turn"}
                    else "completed"
                ),
                "telemetry": {"motion": request["command"]},
            }
        writer.write((json.dumps(response) + "\n").encode())
        await writer.drain()
    writer.close()


def test_remote_adapter_handshake_capabilities_and_goal_translation() -> None:
    async def scenario() -> None:
        requests: list[dict[str, Any]] = []
        server = await asyncio.start_server(
            lambda reader, writer: _body_node(reader, writer, requests),
            "127.0.0.1",
            0,
        )
        port = server.sockets[0].getsockname()[1]
        adapter = RemoteAdapter("secret", port=port, controller_id="voice-test")
        try:
            await adapter.ensure_connected()
            assert adapter.capabilities == {"move", "rotate", "interact"}
            assert await adapter.move(2.0, -0.5, 0.0) == {
                "type": "command_result",
                "command_id": "remote-0001",
                "status": "accepted",
                "telemetry": {"motion": "move"},
            }
            await adapter.turn(45.0)
            await adapter.interact()
            await adapter.stop()
            assert adapter.telemetry() == {"motion": "stop"}
        finally:
            await adapter.close()
            server.close()
            await server.wait_closed()

        assert requests[0]["token"] == "secret"
        assert [request["command"] for request in requests[1:]] == [
            "move",
            "turn",
            "interact",
            "stop",
        ]
        assert all(request["controller_id"] == "voice-test" for request in requests)

    asyncio.run(scenario())


def test_remote_adapter_rejects_unadvertised_required_capabilities() -> None:
    async def scenario() -> None:
        async def incompatible_node(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await reader.readline()
            writer.write(
                (
                    json.dumps(
                        {
                            "type": "handshake_ok",
                            "protocol": "bob-body/0",
                            "capabilities": ["status"],
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(incompatible_node, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        adapter = RemoteAdapter("secret", port=port)
        try:
            with pytest.raises(ValueError, match="missing required capabilities"):
                await adapter.ensure_connected()
        finally:
            await adapter.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_remote_adapter_requires_token() -> None:
    with pytest.raises(ValueError, match="token"):
        RemoteAdapter("")


def test_is_loopback_host() -> None:
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("::1")
    assert not is_loopback_host("localhost")
    assert not is_loopback_host("10.0.0.4")
    assert not is_loopback_host("body.example")


@pytest.mark.parametrize("host", ["localhost", "localhost.", "10.0.0.4"])
def test_remote_adapter_rejects_non_literal_loopback_host(host: str) -> None:
    with pytest.raises(ValueError, match="loopback-only"):
        RemoteAdapter("secret", host=host)


def test_remote_adapter_from_env_rejects_non_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GPTME_VOICE_BODY_URL", "tcp://10.0.0.4:7788")
    monkeypatch.setenv("GPTME_VOICE_BODY_TOKEN", "secret")
    assert body_adapter_from_env() is None


def test_remote_adapter_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GPTME_VOICE_BODY_URL", "tcp://127.0.0.1:7788")
    monkeypatch.setenv("GPTME_VOICE_BODY_TOKEN", "secret")
    monkeypatch.setenv("GPTME_VOICE_BODY_CONTROLLER_ID", "voice-env")
    adapter = body_adapter_from_env()
    assert isinstance(adapter, RemoteAdapter)
    assert adapter.host == "127.0.0.1"
    assert adapter.port == 7788
    assert adapter.controller_id == "voice-env"


def test_remote_adapter_from_env_requires_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GPTME_VOICE_BODY_URL", "tcp://127.0.0.1:7788")
    monkeypatch.delenv("GPTME_VOICE_BODY_TOKEN", raising=False)
    assert body_adapter_from_env() is None


def test_remote_capabilities_register_and_route_interaction() -> None:
    class Adapter:
        name = "remote"
        capabilities = {"interact"}
        requires_startup_connection = False

        async def ensure_connected(self) -> None:
            return None

        async def interact(self) -> dict[str, Any]:
            return {"status": "completed"}

    adapter = Adapter()
    tools = body_tool_schemas(adapter)  # type: ignore[arg-type]
    names = {tool["name"] for tool in tools}
    assert names == {"body_status", "body_interact"}
    interact = next(tool for tool in tools if tool["name"] == "body_interact")
    assert "when the caller wants you to engage" in interact["description"]
    result = asyncio.run(
        GptmeToolBridge(body_adapter=adapter).handle_function_call(  # type: ignore[arg-type]
            "body_interact", {}
        )
    )
    assert result == {"status": "completed"}


def test_remote_adapter_rejects_incompatible_protocol() -> None:
    async def scenario() -> None:
        async def incompatible_node(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await reader.readline()
            writer.write(
                (
                    json.dumps(
                        {
                            "type": "handshake_ok",
                            "protocol": "bob-body/1",
                            "capabilities": [
                                "status",
                                "move",
                                "turn",
                                "stop",
                            ],
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(incompatible_node, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        adapter = RemoteAdapter("secret", port=port)
        try:
            with pytest.raises(ValueError, match="incompatible body protocol"):
                await adapter.ensure_connected()
            assert adapter._writer is None
        finally:
            await adapter.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize("response_type", ["handshake_ok", "command_result"])
def test_remote_adapter_rejects_non_object_telemetry(response_type: str) -> None:
    async def scenario() -> None:
        async def malformed_node(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await reader.readline()
            handshake = dict(_HANDSHAKE_OK)
            if response_type == "handshake_ok":
                handshake["telemetry"] = []
            writer.write((json.dumps(handshake) + "\n").encode())
            await writer.drain()
            if response_type == "command_result":
                request = json.loads(await reader.readline())
                writer.write(
                    (
                        json.dumps(
                            {
                                "type": "command_result",
                                "command_id": request["command_id"],
                                "telemetry": [],
                            }
                        )
                        + "\n"
                    ).encode()
                )
                await writer.drain()
            writer.close()

        server = await asyncio.start_server(malformed_node, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        adapter = RemoteAdapter("secret", port=port)
        try:
            if response_type == "handshake_ok":
                operation = adapter.ensure_connected()
            else:
                await adapter.ensure_connected()
                operation = adapter.stop()
            with pytest.raises(ValueError, match="telemetry must be a JSON object"):
                await operation
            assert adapter._writer is None
        finally:
            await adapter.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_remote_adapter_rejects_non_object_position_telemetry() -> None:
    async def scenario() -> None:
        async def malformed_node(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await reader.readline()
            handshake = dict(_HANDSHAKE_OK)
            handshake["telemetry"] = {"position": "unknown"}
            writer.write((json.dumps(handshake) + "\n").encode())
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(malformed_node, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        adapter = RemoteAdapter("secret", port=port)
        try:
            with pytest.raises(
                ValueError, match="telemetry position must be a JSON object"
            ):
                await adapter.ensure_connected()
            assert adapter._writer is None
        finally:
            await adapter.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_remote_adapter_rejects_mismatched_command_id() -> None:
    async def scenario() -> None:
        async def mismatched_node(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await reader.readline()
            writer.write((json.dumps(_HANDSHAKE_OK) + "\n").encode())
            await writer.drain()
            await reader.readline()
            writer.write(
                (
                    json.dumps(
                        {
                            "type": "command_result",
                            "command_id": "stale-from-earlier",
                            "status": "completed",
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(mismatched_node, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        adapter = RemoteAdapter("secret", port=port)
        try:
            await adapter.ensure_connected()
            with pytest.raises(ValueError, match="command_id"):
                await adapter.move(1.0, 0.0)
            assert adapter._writer is None
        finally:
            await adapter.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_remote_adapter_reconnects_after_timeout() -> None:
    async def scenario() -> None:
        connections = {"n": 0}

        async def flaky_node(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            connections["n"] += 1
            if connections["n"] == 1:
                await reader.readline()
                writer.write((json.dumps(_HANDSHAKE_OK) + "\n").encode())
                await writer.drain()
                await reader.readline()
                await reader.read()
                writer.close()
                return
            await _body_node(reader, writer, [])

        server = await asyncio.start_server(flaky_node, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        adapter = RemoteAdapter("secret", port=port, timeout_s=0.2)
        try:
            await adapter.ensure_connected()
            with pytest.raises(asyncio.TimeoutError):
                await adapter.move(1.0, 0.0)
            assert adapter._writer is None
            result = await adapter.move(1.0, 0.0)
            assert result["command_id"] == "remote-0002"
            assert result["status"] == "accepted"
        finally:
            await adapter.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())
