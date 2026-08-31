from __future__ import annotations

import json
import time

import pytest
from gptme_body_protocol import Command, Handshake, decode_message, encode_message


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
