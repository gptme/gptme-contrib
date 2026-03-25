from __future__ import annotations

from datetime import datetime

import pytest

from gptwitter.api import cached_get_me, parse_time  # type: ignore[import-not-found]


class DummyClient:
    def __init__(self) -> None:
        self.calls = 0

    def get_me(self, user_auth: bool = False):
        self.calls += 1
        return {"user_auth": user_auth}


def test_cached_get_me_reuses_cached_result() -> None:
    cached_get_me.cache_clear()
    client = DummyClient()

    first = cached_get_me(client, user_auth=False)
    second = cached_get_me(client, user_auth=False)

    assert first == {"user_auth": False}
    assert second == {"user_auth": False}
    assert client.calls == 1


def test_parse_time_supports_hours() -> None:
    parsed = parse_time("24h")

    assert isinstance(parsed, datetime)
    assert parsed < datetime.now()


def test_parse_time_supports_days() -> None:
    parsed = parse_time("7d")

    assert isinstance(parsed, datetime)
    assert parsed < datetime.now()


def test_parse_time_supports_isoformat() -> None:
    assert parse_time("2026-03-25T09:00:00") == datetime(2026, 3, 25, 9, 0, 0)


def test_parse_time_invalid_exits() -> None:
    with pytest.raises(SystemExit):
        parse_time("nonsense")
