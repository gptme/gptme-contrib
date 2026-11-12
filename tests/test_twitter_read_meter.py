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
    assert tw.daily_reads_used() == 5


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
