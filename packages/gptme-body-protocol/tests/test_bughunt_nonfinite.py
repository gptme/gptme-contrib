"""Regression tests from the 2026-11-24 verified bug hunt (non-finite JSON)."""

from __future__ import annotations

import json
import math

import pytest
from gptme_body_protocol import Command, decode_message, encode_message

# ---------------------------------------------------------------------------
# R5-3: body protocol emits / accepts non-JSON NaN/Infinity tokens
# ---------------------------------------------------------------------------


def _strict_loads(raw: bytes) -> object:
    def _reject(token: str) -> object:
        raise ValueError(f"non-standard JSON constant {token}")

    return json.loads(raw, parse_constant=_reject)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_r5_3_encode_message_never_emits_invalid_json(value: float) -> None:
    """The protocol is 'one JSON object per framed transport message' consumed by
    native body nodes. A NaN/inf motion arg must be rejected, not framed as the
    bare token NaN/Infinity, which a strict JSON parser rejects (and which is a
    nonsensical motion goal)."""
    cmd = Command("move-1", "voice", "move", args={"forward_m": value})
    try:
        frame = encode_message(cmd)
    except ValueError:
        return  # refusing to encode is an acceptable fix
    _strict_loads(frame)  # must be valid RFC 8259 JSON


def test_r5_3_decode_message_rejects_nan_constants() -> None:
    """A body-node response carrying NaN telemetry is not valid JSON; decode
    should reject it rather than hand float('nan') positions to the runtime."""
    line = (
        b'{"type":"command_result","command_id":"remote-0001",'
        b'"telemetry":{"position":{"x":NaN,"y":Infinity}}}\n'
    )
    with pytest.raises(ValueError):
        decode_message(line)
