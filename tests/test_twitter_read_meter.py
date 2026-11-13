from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts" / "twitter"


@pytest.fixture()
def tw(monkeypatch, tmp_path):
    pytest.importorskip("tweepy")
    pytest.importorskip("gptmail")
    monkeypatch.syspath_prepend(str(SCRIPT_DIR))
    monkeypatch.setenv("TWITTER_READ_COUNTER_DIR", str(tmp_path))
    monkeypatch.delenv("TWITTER_ACCOUNT", raising=False)
    monkeypatch.delenv("TWITTER_DAILY_READ_LIMIT", raising=False)
    spec = importlib.util.spec_from_file_location(
        "twitter_meter_under_test", SCRIPT_DIR / "twitter.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop(spec.name, None)


def test_count_billable_reads(tw):
    payload = {
        "data": [{"id": "1"}, {"id": "2"}],
        "includes": {"tweets": [{"id": "3"}], "users": [{"id": "u"}]},
    }
    assert tw.count_billable_reads(payload) == 2 + 1 + 2  # users cost 2x
    assert tw.count_billable_reads({"data": {"id": "1"}}) == 1
    assert tw.count_billable_reads({"meta": {"result_count": 0}}) == 0
    assert tw.count_billable_reads(None) == 0


def test_meter_counts_gets_and_blocks_at_limit(tw, monkeypatch):
    calls = []

    def fake_request(self, method, route, *a, **k):
        calls.append((method, route))
        return SimpleNamespace(ok=True, json=lambda: {"data": [{"id": "1"}] * 4})

    monkeypatch.setattr(tw.tweepy.client.BaseClient, "request", fake_request)
    tw.install_read_meter()
    tw.install_read_meter()  # idempotent: must not double count
    monkeypatch.setenv("TWITTER_DAILY_READ_LIMIT", "10")
    client = tw.tweepy.Client(bearer_token="x")

    for _ in range(3):  # 4 + 4 + 4: the check runs before each request
        client.request("GET", "/2/tweets/search/recent")
    assert tw.daily_reads_used() == 12
    with pytest.raises(tw.DailyReadLimitReached):
        client.request("GET", "/2/tweets/search/recent")
    assert len(calls) == 3  # blocked read never reached the network
    client.request("POST", "/2/tweets")  # writes are neither counted nor blocked
    assert tw.daily_reads_used() == 12


def test_unlimited_by_default_but_still_counts(tw, monkeypatch):
    monkeypatch.setattr(
        tw.tweepy.client.BaseClient,
        "request",
        lambda self, m, r, *a, **k: SimpleNamespace(
            ok=True, json=lambda: {"data": [{"id": "1"}]}
        ),
    )
    tw.install_read_meter()
    client = tw.tweepy.Client(bearer_token="x")
    for _ in range(5):
        client.request("GET", "/2/users/me")
    assert tw.daily_read_limit() == 0
    assert tw.daily_reads_used() == 10


def test_failed_response_is_not_counted(tw, monkeypatch):
    monkeypatch.setattr(
        tw.tweepy.client.BaseClient,
        "request",
        lambda self, m, r, *a, **k: SimpleNamespace(
            ok=False, json=lambda: {"data": [{"id": "1"}]}
        ),
    )
    tw.install_read_meter()
    tw.tweepy.Client(bearer_token="x").request("GET", "/2/tweets")
    assert tw.daily_reads_used() == 0


@pytest.mark.parametrize("return_type", ["response", "dict", "http"])
def test_meter_wraps_http_response_before_tweepy_conversion(
    tw, monkeypatch, return_type
):
    """Exercise real BaseClient.request and _make_request, not a request stub."""
    import requests

    types = {
        "response": tw.tweepy.Response,
        "dict": dict,
        "http": requests.Response,
    }
    client = tw.tweepy.Client(bearer_token="x", return_type=types[return_type])
    calls = []

    def transport(method, url, **kwargs):
        calls.append((method, url))
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"data": [{"id": "1", "text": "hello", "edit_history_tweet_ids": ["1"]}], "includes": {"users": [{"id": "2", "name": "Bot", "username": "bot"}]}}'
        return response

    monkeypatch.setattr(client.session, "request", transport)
    # Restore the real request on teardown, even though installation patches it.
    monkeypatch.setattr(
        tw.tweepy.client.BaseClient, "request", tw.tweepy.client.BaseClient.request
    )
    monkeypatch.setenv("TWITTER_DAILY_READ_LIMIT", "3")
    tw.install_read_meter()
    result = client.get_tweets(ids=[1])
    assert isinstance(result, types[return_type])
    assert tw.daily_reads_used() == 3
    with pytest.raises(tw.DailyReadLimitReached):
        client.get_tweets(ids=[1])
    assert len(calls) == 1


@pytest.mark.parametrize(
    "route",
    [
        "/2/users/me",
        "/2/users",
        "/2/users/by",
        "/2/users/by/username/bot",
        "/2/users/2/followers",
        "/2/tweets/1/liking_users",
    ],
)
def test_meter_weights_top_level_users(tw, monkeypatch, route):
    monkeypatch.setattr(
        tw.tweepy.client.BaseClient,
        "request",
        lambda *a, **k: SimpleNamespace(
            ok=True, json=lambda: {"data": [{"id": "2", "username": "bot"}]}
        ),
    )
    tw.install_read_meter()
    tw.tweepy.Client(bearer_token="x").request("GET", route)
    assert tw.daily_reads_used() == 2


def test_read_budget_loads_dotenv_without_network(tw, monkeypatch, tmp_path):
    from click.testing import CliRunner
    from dotenv import load_dotenv

    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    env = tmp_path / ".env"
    counter_dir = tmp_path / "configured-counter"
    env.write_text(
        f"TWITTER_DAILY_READ_LIMIT=10\nTWITTER_READ_COUNTER_DIR={counter_dir}\nTWITTER_ACCOUNT=other\n"
    )
    monkeypatch.setenv("TWITTER_ACCOUNT", "")  # explicit default wins over .env
    monkeypatch.setattr(tw, "load_dotenv", lambda **kw: load_dotenv(env, **kw))
    counter_dir.mkdir()
    day = tw.datetime.now(tw.timezone.utc).strftime("%Y-%m-%d")
    (counter_dir / f"{day}.json").write_text('{"reads":10}')
    result = CliRunner().invoke(tw.cli, ["read-budget"])
    assert result.exit_code == 1, result.output
    assert "10/10 billable reads" in result.output


def test_concurrent_reads_share_admission_lock(tw, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()
    calls = []

    def request(*a, **k):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(ok=True, json=lambda: {"data": {"id": "1"}})

    monkeypatch.setattr(tw.tweepy.client.BaseClient, "request", request)
    monkeypatch.setenv("TWITTER_DAILY_READ_LIMIT", "1")
    tw.install_read_meter()
    client = tw.tweepy.Client(bearer_token="x")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.request, "GET", "/2/tweets/1")
        assert entered.wait(5)
        second = pool.submit(client.request, "GET", "/2/tweets/1")
        release.set()
        first.result(timeout=5)
        with pytest.raises(tw.DailyReadLimitReached):
            second.result(timeout=5)
    assert calls == [1]
    assert tw.daily_reads_used() == 1


@pytest.mark.parametrize("oauth2", [False, True])
def test_exhausted_budget_still_validates_identity_and_posts(tw, monkeypatch, oauth2):
    import requests

    monkeypatch.setattr(tw, "load_dotenv", lambda **kw: None)
    monkeypatch.setenv("TWITTER_ACCOUNT", "")
    for key in (
        "BEARER_TOKEN",
        "API_KEY",
        "API_SECRET",
        "ACCESS_TOKEN",
        "ACCESS_SECRET",
    ):
        monkeypatch.setenv(f"TWITTER_{key}", "test")
    monkeypatch.setenv("TWITTER_EXPECTED_USERNAME", "bot")
    monkeypatch.delenv("TWITTER_CLIENT_ID", raising=False)
    monkeypatch.delenv("TWITTER_CLIENT_SECRET", raising=False)
    if oauth2:
        monkeypatch.setenv("TWITTER_CLIENT_ID", "test")
        monkeypatch.setenv("TWITTER_CLIENT_SECRET", "test")
        monkeypatch.setenv("TWITTER_OAUTH2_ACCESS_TOKEN", "test")
        monkeypatch.setenv("TWITTER_OAUTH2_REFRESH_TOKEN", "test")
        monkeypatch.setenv("TWITTER_OAUTH2_EXPIRES_AT", "2099-01-01T00:00:00+00:00")
    tw._read_counter_path().write_text('{"reads":10}')
    monkeypatch.setenv("TWITTER_DAILY_READ_LIMIT", "10")
    calls = []

    def transport(self, method, url, **kwargs):
        calls.append((method, url))
        response = requests.Response()
        response.status_code = 200
        response._content = (
            b'{"data":{"id":"2","name":"Bot","username":"bot"}}'
            if method == "GET"
            else b'{"data":{"id":"3","text":"hello"}}'
        )
        return response

    monkeypatch.setattr(requests.Session, "request", transport)
    monkeypatch.setattr(
        tw.tweepy.client.BaseClient, "request", tw.tweepy.client.BaseClient.request
    )
    client = tw.load_twitter_client(require_auth=True, headless=True)
    client.create_tweet(text="hello", user_auth=tw._get_user_auth(client))
    assert tw.daily_reads_used() == 12  # identity check is still billed
    with pytest.raises(tw.DailyReadLimitReached):
        client.get_tweets(ids=[1])
    assert [method for method, _ in calls] == ["GET", "POST"]


@pytest.mark.parametrize(
    "route",
    [
        "/2/users/2/tweets",
        "/2/users/2/mentions",
        "/2/users/2/liked_tweets",
        "/2/users/2/bookmarks",
        "/2/users/2/timelines/reverse_chronological",
    ],
)
def test_user_timelines_still_count_posts(tw, route):
    assert tw.count_billable_reads({"data": [{"id": "1"}]}, route) == 1


def test_recursive_rate_limit_retry_counts_once_without_deadlock(tw, monkeypatch):
    import requests

    client = tw.tweepy.Client(bearer_token="x", wait_on_rate_limit=True)
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        response = requests.Response()
        response.status_code = 429 if len(calls) == 1 else 200
        response.headers["x-rate-limit-reset"] = "0"
        response._content = b'{"data":[{"id":"1","text":"hello"}]}'
        return response

    monkeypatch.setattr(client.session, "request", transport)
    monkeypatch.setattr(tw.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        tw.tweepy.client.BaseClient, "request", tw.tweepy.client.BaseClient.request
    )
    tw.install_read_meter()
    client.get_tweets(ids=[1])
    assert len(calls) == 2
    assert tw.daily_reads_used() == 1


def test_read_budget_uses_named_profile_configuration(tw, monkeypatch, tmp_path):
    from click.testing import CliRunner
    from dotenv import load_dotenv

    monkeypatch.setattr(
        tw, "load_dotenv", lambda *a, **kw: load_dotenv(*a, **kw) if a else None
    )
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    profile = tw.account_env_path("bot")
    profile.parent.mkdir(parents=True)
    counter_dir = tmp_path / "profile-counter"
    profile.write_text(
        f"TWITTER_DAILY_READ_LIMIT=4\nTWITTER_READ_COUNTER_DIR={counter_dir}\n"
    )
    counter_dir.mkdir()
    day = tw.datetime.now(tw.timezone.utc).strftime("%Y-%m-%d")
    (counter_dir / f"{day}-bot.json").write_text('{"reads":4}')
    result = CliRunner().invoke(tw.cli, ["--account", "bot", "read-budget"])
    assert result.exit_code == 1, result.output
    assert "4/4 billable reads" in result.output


@pytest.mark.parametrize("rollover_stage", ["lock", "retry"])
def test_midnight_rollover_uses_current_day(tw, monkeypatch, rollover_stage):
    import json
    from datetime import datetime, timezone

    import requests

    clock = [datetime(2026, 11, 12, 23, 59, tzinfo=timezone.utc)]

    class Clock:
        @staticmethod
        def now(tz):
            return clock[0]

    monkeypatch.setattr(tw, "datetime", Clock)
    yesterday = tw._read_counter_path()
    yesterday.write_text('{"reads":1}')
    monkeypatch.setenv("TWITTER_DAILY_READ_LIMIT", "1")
    today = tw._read_counter_path("2026-11-13")
    original_flock = tw.fcntl.flock

    def flock(fd, operation):
        original_flock(fd, operation)
        if rollover_stage == "lock":
            clock[0] = datetime(2026, 11, 13, tzinfo=timezone.utc)

    monkeypatch.setattr(tw.fcntl, "flock", flock)
    if rollover_stage == "retry":
        yesterday.write_text('{"reads":0}')
    calls = []
    client = tw.tweepy.Client(bearer_token="x", wait_on_rate_limit=True)

    def transport(*args, **kwargs):
        calls.append(1)
        response = requests.Response()
        response.status_code = (
            429 if rollover_stage == "retry" and len(calls) == 1 else 200
        )
        response.headers["x-rate-limit-reset"] = "0"
        response._content = b'{"data":[{"id":"1","text":"hello"}]}'
        return response

    monkeypatch.setattr(client.session, "request", transport)
    monkeypatch.setattr(tw.time, "time", lambda: 0)
    monkeypatch.setattr(
        tw.time,
        "sleep",
        lambda _: clock.__setitem__(0, datetime(2026, 11, 13, tzinfo=timezone.utc)),
    )
    monkeypatch.setattr(
        tw.tweepy.client.BaseClient, "request", tw.tweepy.client.BaseClient.request
    )
    tw.install_read_meter()
    client.get_tweets(ids=[1])
    assert json.loads(today.read_text()) == {"reads": 1}
    assert json.loads(yesterday.read_text()) == {
        "reads": 1 if rollover_stage == "lock" else 0
    }
    with pytest.raises(tw.DailyReadLimitReached):
        client.get_tweets(ids=[1])
    assert len(calls) == (1 if rollover_stage == "lock" else 2)


def test_failed_counter_replacement_preserves_previous_usage(tw, monkeypatch):
    tw._read_counter_path().write_text('{"reads":9}')
    monkeypatch.setattr(
        tw.tweepy.client.BaseClient,
        "request",
        lambda *a, **kw: SimpleNamespace(ok=True, json=lambda: {"data": {"id": "1"}}),
    )

    def interrupted_replace(*args):
        raise OSError("simulated interruption before atomic replacement")

    monkeypatch.setattr(tw.os, "replace", interrupted_replace)
    tw.install_read_meter()
    with pytest.raises(OSError, match="simulated interruption"):
        tw.tweepy.Client(bearer_token="x").request("GET", "/2/tweets/1")
    assert tw.daily_reads_used() == 9
