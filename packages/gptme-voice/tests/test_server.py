import asyncio
import base64
import json
import tempfile
from pathlib import Path

from gptme_voice.realtime.server import (
    VoiceServer,
    _build_caller_instructions,
    _get_twilio_field,
)


class _DummyWebSocket:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_text(self, message: str) -> None:
        self.messages.append(message)


def test_build_caller_instructions_no_number() -> None:
    base = "You are Bob."
    result = _build_caller_instructions(base, "", None)
    assert result == base


def test_build_caller_instructions_unknown_number() -> None:
    result = _build_caller_instructions("You are Bob.", "+15551234567", None)
    assert "+15551234567" in result
    assert "unknown" in result.lower()
    assert "You are Bob." in result


def test_build_caller_instructions_known_number_from_people_dir() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        people_dir = Path(tmpdir) / "people"
        people_dir.mkdir()
        (people_dir / "erik-bjareholt.md").write_text(
            "# Erik Bjäreholt\n\nPhone: +46765784797\n"
        )
        result = _build_caller_instructions("You are Bob.", "+46765784797", tmpdir)
    assert "Erik Bjäreholt" in result
    assert "+46765784797" in result
    assert "You are Bob." in result


def test_get_twilio_field_prefers_camel_case() -> None:
    payload = {"streamSid": "MZ123", "stream_sid": "legacy"}

    assert _get_twilio_field(payload, "streamSid", "stream_sid") == "MZ123"


def test_get_twilio_field_falls_back_to_snake_case() -> None:
    payload = {"stream_sid": "legacy"}

    assert _get_twilio_field(payload, "streamSid", "stream_sid") == "legacy"


def test_send_to_twilio_uses_stream_sid_field_name() -> None:
    server = VoiceServer()
    websocket = _DummyWebSocket()

    asyncio.run(server._send_to_twilio(websocket, "MZ123", b"\x00\x01"))

    assert len(websocket.messages) == 1
    message = json.loads(websocket.messages[0])
    assert message == {
        "event": "media",
        "streamSid": "MZ123",
        "media": {"payload": base64.b64encode(b"\x00\x01").decode("utf-8")},
    }
