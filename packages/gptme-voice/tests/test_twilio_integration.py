from click.testing import CliRunner
from gptme_voice.realtime.call import main as call_main
from gptme_voice.realtime.twilio_integration import (
    OutboundCallSettings,
    build_connect_stream_twiml,
    build_stream_url,
    create_outbound_call,
    resolve_outbound_call_settings,
)


def test_build_stream_url_normalizes_https_base_url():
    assert build_stream_url("https://voice.example") == "wss://voice.example/twilio"


def test_build_stream_url_accepts_bare_host_with_path():
    assert (
        build_stream_url("voice.example/realtime")
        == "wss://voice.example/realtime/twilio"
    )


def test_build_connect_stream_twiml_embeds_stream_url():
    twiml = build_connect_stream_twiml("wss://voice.example/twilio")

    assert '<Stream url="wss://voice.example/twilio" />' in twiml
    assert twiml.startswith('<?xml version="1.0" encoding="UTF-8"?>')


def test_build_connect_stream_twiml_escapes_url():
    twiml = build_connect_stream_twiml(
        'wss://voice.example/twilio?token="abc"&mode=fast'
    )

    assert "&quot;abc&quot;" in twiml
    assert "&amp;mode=fast" in twiml


def test_resolve_outbound_call_settings_uses_config_fallbacks(monkeypatch):
    values = {
        "TWILIO_ACCOUNT_SID": "AC123",
        "TWILIO_AUTH_TOKEN": "secret",
        "TWILIO_PHONE_NUMBER": "+15551234567",
        "TWILIO_PUBLIC_BASE_URL": "https://voice.example",
    }
    monkeypatch.setattr(
        "gptme_voice.realtime.twilio_integration._get_config_env",
        lambda name: values.get(name),
    )

    settings = resolve_outbound_call_settings()

    assert settings == OutboundCallSettings(
        account_sid="AC123",
        auth_token="secret",
        from_number="+15551234567",
        stream_url="wss://voice.example/twilio",
    )


def test_create_outbound_call_uses_twilio_client():
    captured = {}

    class FakeCalls:
        def create(self, **kwargs):
            captured.update(kwargs)

            class Response:
                sid = "CA123"

            return Response()

    class FakeClient:
        def __init__(self, account_sid, auth_token):
            captured["account_sid"] = account_sid
            captured["auth_token"] = auth_token
            self.calls = FakeCalls()

    settings = OutboundCallSettings(
        account_sid="AC123",
        auth_token="secret",
        from_number="+15551234567",
        stream_url="wss://voice.example/twilio",
    )

    sid = create_outbound_call("+46701234567", settings, client_cls=FakeClient)

    assert sid == "CA123"
    assert captured == {
        "account_sid": "AC123",
        "auth_token": "secret",
        "to": "+46701234567",
        "from_": "+15551234567",
        "twiml": build_connect_stream_twiml("wss://voice.example/twilio"),
    }


def test_call_cli_dry_run_prints_twiml(monkeypatch):
    monkeypatch.setattr(
        "gptme_voice.realtime.call.resolve_outbound_call_settings",
        lambda **_: OutboundCallSettings(
            account_sid="AC123",
            auth_token="secret",
            from_number="+15551234567",
            stream_url="wss://voice.example/twilio",
        ),
    )

    result = CliRunner().invoke(
        call_main,
        ["+46701234567", "--dry-run"],
    )

    assert result.exit_code == 0
    assert '<Stream url="wss://voice.example/twilio" />' in result.output
