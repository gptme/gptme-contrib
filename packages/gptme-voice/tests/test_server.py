import asyncio
import base64
import json

from gptme_voice.realtime.server import (
    CallContext,
    VoiceServer,
    _build_call_instructions,
    _get_twilio_field,
    _is_allowed_caller,
    _normalize_phone_number,
    _parse_allowed_callers,
)
from starlette.testclient import TestClient


class _DummyWebSocket:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_text(self, message: str) -> None:
        self.messages.append(message)


class _StartOnlyWebSocket:
    def __init__(self, message: dict) -> None:
        self.message = json.dumps(message)
        self.accepted = False
        self.closed = False
        self.close_code: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int) -> None:
        self.closed = True
        self.close_code = code

    async def iter_text(self):
        yield self.message


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


def test_normalize_phone_number_keeps_e164_format() -> None:
    assert _normalize_phone_number(" +46 765-784 797 ") == "+46765784797"


def test_parse_allowed_callers_normalizes_comma_separated_numbers() -> None:
    assert _parse_allowed_callers("+46 765 784 797, +1 (555) 100-2000") == {
        "+46765784797",
        "+15551002000",
    }


def test_is_allowed_caller_denies_missing_number_when_allowlist_enabled() -> None:
    assert not _is_allowed_caller({"+46765784797"}, None, default=False)


def test_build_call_instructions_includes_caller_metadata() -> None:
    context = CallContext(
        call_sid="CA123",
        from_number="+46765784797",
        to_number="+15551234567",
        caller_name="Erik",
    )

    instructions = _build_call_instructions("Base instructions", context)

    assert "Caller phone number: +46765784797" in instructions
    assert "Caller name from Twilio: Erik" in instructions
    assert "Do not claim that you cannot see the caller's number" in instructions


def test_build_session_config_injects_call_metadata(monkeypatch) -> None:
    monkeypatch.setattr(
        "gptme_voice.realtime.server._get_config_env",
        lambda name: None,
    )
    server = VoiceServer(workspace=None)
    context = CallContext(call_sid="CA123", from_number="+46765784797")

    session_cfg = server._build_session_config(context)

    assert "Caller phone number: +46765784797" in session_cfg.instructions


def test_incoming_call_rejects_non_allowlisted_caller(monkeypatch) -> None:
    env = {
        "TWILIO_ALLOWED_CALLERS": "+46765784797",
        "GPTME_VOICE_PUBLIC_BASE_URL": "https://voice.example.com",
    }
    monkeypatch.setattr(
        "gptme_voice.realtime.server._get_config_env",
        lambda name: env.get(name),
    )
    server = VoiceServer(workspace=None)
    client = TestClient(server.app)

    response = client.post(
        "/incoming",
        data={"CallSid": "CA999", "From": "+15551234567", "To": "+46700000000"},
    )

    assert response.status_code == 200
    assert "<Reject />" in response.text
    assert "CA999" not in server._call_contexts


def test_incoming_call_records_allowlisted_context(monkeypatch) -> None:
    env = {
        "TWILIO_ALLOWED_CALLERS": "+46765784797",
        "GPTME_VOICE_PUBLIC_BASE_URL": "https://voice.example.com",
    }
    monkeypatch.setattr(
        "gptme_voice.realtime.server._get_config_env",
        lambda name: env.get(name),
    )
    server = VoiceServer(workspace=None)
    client = TestClient(server.app)

    response = client.post(
        "/incoming",
        data={
            "CallSid": "CA123",
            "From": "+46 765 784 797",
            "To": "+46700000000",
            "CallerName": "Erik",
        },
    )

    assert response.status_code == 200
    assert 'Stream url="wss://voice.example.com/twilio"' in response.text
    assert server._call_contexts["CA123"] == CallContext(
        call_sid="CA123",
        from_number="+46765784797",
        to_number="+46700000000",
        caller_name="Erik",
    )


def test_twilio_websocket_rejects_untracked_call_when_allowlist_enabled(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "gptme_voice.realtime.server._get_config_env",
        lambda name: None,
    )
    server = VoiceServer(workspace=None)
    server._allowed_callers = {"+46765784797"}
    websocket = _StartOnlyWebSocket(
        {"event": "start", "start": {"streamSid": "MZ123", "callSid": "CA123"}}
    )

    called = False

    def _unexpected_make_client(*args, **kwargs):  # pragma: no cover - sanity guard
        nonlocal called
        called = True
        raise AssertionError("realtime client should not be created")

    monkeypatch.setattr(server, "_make_client", _unexpected_make_client)

    asyncio.run(server.handle_twilio_websocket(websocket))

    assert websocket.accepted
    assert websocket.closed
    assert websocket.close_code == 1008
    assert not called
