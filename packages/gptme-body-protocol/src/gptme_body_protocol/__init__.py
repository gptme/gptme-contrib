"""Transport-neutral wire models for gptme body nodes.

The protocol is intentionally small: one JSON object per framed transport
message. Body nodes and clients share these DTOs without importing either the
voice runtime or a body-specific driver.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

PROTOCOL_VERSION = "bob-body/0"


@dataclass(frozen=True)
class Handshake:
    """Authenticate a controller and negotiate one body-node session."""

    token: str
    controller_id: str
    protocol: str = PROTOCOL_VERSION
    type: Literal["handshake"] = "handshake"


@dataclass(frozen=True)
class Command:
    """A bounded, idempotent body goal."""

    command_id: str
    controller_id: str
    command: Literal["status", "move", "turn", "stop", "interact"]
    args: dict[str, Any] = field(default_factory=dict)
    ttl_ms: int = 2_000
    sent_at_ms: int = field(default_factory=lambda: int(time.time() * 1_000))
    type: Literal["command"] = "command"


def encode_message(message: Handshake | Command) -> bytes:
    """Encode one newline-framed protocol message."""
    return (json.dumps(asdict(message), separators=(",", ":")) + "\n").encode()


def decode_message(line: bytes) -> dict[str, Any]:
    """Decode one response, rejecting non-object JSON."""
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("body-node response must be a JSON object")
    return value


__all__ = [
    "PROTOCOL_VERSION",
    "Command",
    "Handshake",
    "decode_message",
    "encode_message",
]
