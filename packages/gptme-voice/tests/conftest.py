from datetime import datetime, timezone

import pytest

# Midday UTC, so test offsets of minutes or a few hours never cross a date.
PINNED_UTC_NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


class _PinnedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return PINNED_UTC_NOW.replace(tzinfo=None)
        return PINNED_UTC_NOW.astimezone(tz)


@pytest.fixture
def pinned_utc_now(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Pin ``datetime.now()`` for the callback loader and the requesting test module.

    The callback loader requires the call, its prepared context and "now" to
    share a UTC calendar date. Tests that build fixtures from the wall clock
    with minute offsets therefore failed whenever CI ran just after 00:00 UTC.
    """
    from gptme_voice.realtime import missed_call_context

    monkeypatch.setattr(missed_call_context, "datetime", _PinnedDatetime)
    monkeypatch.setattr(request.module, "datetime", _PinnedDatetime)
    return PINNED_UTC_NOW


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
