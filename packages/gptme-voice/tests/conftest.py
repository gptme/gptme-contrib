import pytest


@pytest.fixture(autouse=True)
def _no_host_twilio_auth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the host's real TWILIO_AUTH_TOKEN out of server tests.

    With a token configured, /twilio rejects start events that lack a valid
    stream token. Tests that exercise that path set the token explicitly.
    """
    import gptme_voice.realtime.server as server_mod

    real_get = server_mod._get_config_env

    def _get(name: str) -> str | None:
        if name == "TWILIO_AUTH_TOKEN":
            return None
        return real_get(name)

    monkeypatch.setattr(server_mod, "_get_config_env", _get)
