from __future__ import annotations

import json
import time

import pytest
from gptme_body_protocol import (
    Command,
    Handshake,
    decode_message,
    encode_message,
    require_command_result,
    require_handshake_ok,
)


def test_protocol_frames_handshake_and_command() -> None:
    handshake = json.loads(encode_message(Handshake("secret", "voice")))
    assert handshake == {
        "token": "secret",
        "controller_id": "voice",
        "protocol": "bob-body/0",
        "type": "handshake",
    }

    command = Command(
        command_id="move-1",
        controller_id="voice",
        command="move",
        args={"forward_m": 1.5},
        ttl_ms=250,
        sent_at_ms=123,
    )
    assert encode_message(command).endswith(b"\n")
    assert decode_message(encode_message(command))["command_id"] == "move-1"


def test_command_timestamp_is_current() -> None:
    command = Command("status-1", "voice", "status")
    assert abs(command.sent_at_ms - int(time.time() * 1_000)) < 1_000


def test_decode_rejects_non_object_json() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        decode_message(b"[]\n")


def test_decode_rejects_incompatible_protocol() -> None:
    with pytest.raises(ValueError, match="incompatible body protocol"):
        decode_message(b'{"type":"handshake_ok","protocol":"bob-body/1"}\n')


def test_require_handshake_ok_rejects_missing_protocol() -> None:
    with pytest.raises(ValueError, match="incompatible body protocol"):
        require_handshake_ok({"type": "handshake_ok", "capabilities": ["status"]})


def test_require_command_result_rejects_mismatched_id() -> None:
    with pytest.raises(ValueError, match="command_id"):
        require_command_result(
            {"type": "command_result", "command_id": "other"},
            "remote-0001",
        )


def test_require_command_result_allows_omitted_protocol() -> None:
    """Handshake is the version gate; native nodes omit protocol on results."""
    require_command_result(
        {"type": "command_result", "command_id": "remote-0001"},
        "remote-0001",
    )
