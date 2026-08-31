"""Remote BodyAdapter over the versioned gptme body-node protocol."""

from __future__ import annotations

import asyncio
import ipaddress
from typing import Any

from gptme_body_protocol import (
    Command,
    Handshake,
    decode_message,
    encode_message,
    require_command_result,
    require_handshake_ok,
)

# Body-node wire capabilities mapped to gptme-voice's model-facing capabilities.
_WIRE_TO_ADAPTER_CAPABILITY = {
    "move": "move",
    "turn": "rotate",
    "interact": "interact",
}
_REQUIRED_WIRE_CAPABILITIES = {"status", "move", "turn", "stop"}


def is_loopback_host(host: str) -> bool:
    """True for literal loopback addresses and localhost.

    Hostnames are not resolved. Plaintext body control is local-only, so a
    DNS name other than localhost is treated as non-loopback.
    """
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class RemoteAdapter:
    """Translate model-facing body goals into authenticated body-node messages.

    The body node owns controller leases, command deadlines, idempotency, and
    local safety. This adapter owns framing, handshake negotiation, and the
    translation from the stable voice tool contract to the neutral wire DTOs.
    """

    name = "remote"
    requires_startup_connection = True

    def __init__(
        self,
        token: str,
        *,
        host: str = "127.0.0.1",
        port: int = 7777,
        controller_id: str = "gptme-voice-local",
        timeout_s: float = 2.0,
    ) -> None:
        if not token:
            raise ValueError("a non-empty body token is required")
        if not is_loopback_host(host):
            raise ValueError(
                f"plaintext body transport is loopback-only, refused host {host!r}"
            )
        self.token = token
        self.host = host
        self.port = port
        self.controller_id = controller_id
        self.timeout_s = timeout_s
        self.capabilities: set[str] = set()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._sequence = 0
        self._telemetry: dict[str, Any] = {}
        self._command_lock = asyncio.Lock()
        self._connection_lock = asyncio.Lock()

    def _is_connected(self) -> bool:
        return (
            self._reader is not None
            and self._writer is not None
            and not self._writer.is_closing()
        )

    async def ensure_connected(self) -> None:
        if self._is_connected():
            return
        async with self._connection_lock:
            if self._is_connected():
                return
            if self._writer is not None:
                await self.close()
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout_s
            )
            try:
                response = await self._exchange(
                    Handshake(token=self.token, controller_id=self.controller_id)
                )
                wire_capabilities = set(response.get("capabilities") or ())
                missing = _REQUIRED_WIRE_CAPABILITIES - wire_capabilities
                if missing:
                    raise ValueError(
                        "body handshake missing required capabilities: "
                        + ", ".join(sorted(missing))
                    )
                self.capabilities = {
                    adapter_capability
                    for wire_capability, adapter_capability in _WIRE_TO_ADAPTER_CAPABILITY.items()
                    if wire_capability in wire_capabilities
                }
                self._telemetry = response.get("telemetry") or {}
            except BaseException:
                await self.close()
                raise

    async def close(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        self.capabilities = set()
        if writer is not None:
            writer.close()
            await writer.wait_closed()

    async def move(
        self, forward_m: float, right_m: float, up_m: float = 0.0
    ) -> dict[str, Any]:
        if up_m:
            return {"error": "This body has no altitude capability."}
        return await self._command("move", {"forward_m": forward_m, "right_m": right_m})

    async def turn(self, yaw_deg: float) -> dict[str, Any]:
        return await self._command("turn", {"yaw_deg": yaw_deg})

    async def stop(self) -> dict[str, Any]:
        return await self._command("stop")

    async def interact(self) -> dict[str, Any]:
        return await self._command("interact")

    def telemetry(self) -> dict[str, Any]:
        return self._telemetry.copy()

    async def takeoff(self, altitude_m: float) -> dict[str, Any]:
        return {"error": "This body has no takeoff capability."}

    async def land(self) -> dict[str, Any]:
        return {"error": "This body has no land capability."}

    async def goto(
        self,
        latitude_deg: float,
        longitude_deg: float,
        altitude_m: float | None,
    ) -> dict[str, Any]:
        return {"error": "This body has no GPS capability."}

    async def return_home(self) -> dict[str, Any]:
        return {"error": "This body has no return-home capability."}

    async def _command(
        self, command: str, args: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        async with self._command_lock:
            await self.ensure_connected()
            self._sequence += 1
            message = Command(
                command_id=f"remote-{self._sequence:04d}",
                controller_id=self.controller_id,
                command=command,  # type: ignore[arg-type]
                args=args or {},
            )
            response = await self._exchange(message)
            if "telemetry" in response:
                self._telemetry = response["telemetry"]
            return response

    async def _exchange(self, message: Handshake | Command) -> dict[str, Any]:
        reader, writer = self._reader, self._writer
        if reader is None or writer is None:
            raise ConnectionError("body node is not connected")
        try:
            writer.write(encode_message(message))
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), self.timeout_s)
            if not line:
                raise ConnectionError("body node closed the connection")
            response = decode_message(line)
            if isinstance(message, Handshake):
                require_handshake_ok(response)
            else:
                require_command_result(response, message.command_id)
            return response
        except BaseException:
            await self.close()
            raise
