"""AW_SERVER parsing and endpoint consistency tests."""

from datetime import date
from contextlib import nullcontext
from io import BytesIO
from unittest.mock import patch

import pytest

from gptme_activity_summary import aw_data


@pytest.mark.parametrize(
    ("server", "expected"),
    [
        ("http://localhost:5600", "http://localhost:5600"),
        ("http://localhost:5600/", "http://localhost:5600"),
        ("http://aw.example.test:5600", "http://aw.example.test:5600"),
        ("http://aw.example.test", "http://aw.example.test:80"),
        ("https://aw.example.test", "https://aw.example.test:443"),
        ("https://aw.example.test:8443", "https://aw.example.test:8443"),
        ("http://192.0.2.10:5600", "http://192.0.2.10:5600"),
        ("http://[::1]:5600", "http://[::1]:5600"),
        ("https://[::1]", "https://[::1]:443"),
        ("http://[fe80::1%25eth0]:5600", "http://[fe80::1%25eth0]:5600"),
        ("https://[2001:db8::1]:8443", "https://[2001:db8::1]:8443"),
    ],
)
def test_client_and_settings_use_same_server_address(monkeypatch, server, expected):
    monkeypatch.setattr(aw_data, "AW_SERVER", server)
    with patch("aw_client.client.SingleInstance"):
        client = aw_data._get_client()

    assert client.server_address == expected

    settings_url = f"{expected}/api/0/settings/classes"
    with patch(
        "gptme_activity_summary.aw_data.urlopen",
        return_value=nullcontext(BytesIO(b"[]")),
    ) as open_url:
        assert aw_data._fetch_category_rules() == []
    open_url.assert_called_once_with(settings_url, timeout=aw_data.AW_TIMEOUT)


@pytest.mark.parametrize(
    "server",
    [
        "ftp://aw.example.test:5600",
        "//aw.example.test:5600",
        "http:///missing-host",
        "http://",
        "http://aw.example.test/path",
        "http://aw.example.test/?query=1",
        "http://aw.example.test/?",
        "http://aw.example.test/#fragment",
        "http://aw.example.test/#",
        "http://user:secret@aw.example.test:5600",
        "http://aw.example.test:",
        "http://aw.example.test:0",
        "http://aw.example.test:65536",
        "http://aw.example.test:not-a-port",
        "http://bad host:5600",
        "http://aw.example.test:5600\n",
        "http://[::1:5600",
        "http://[::1]garbage:5600",
        "http://[::1]garbage",
    ],
)
def test_invalid_server_never_creates_client_or_fetches_settings(monkeypatch, caplog, server):
    monkeypatch.setattr(aw_data, "AW_SERVER", server)
    client_factory = patch("aw_client.ActivityWatchClient")
    urlopen = patch("gptme_activity_summary.aw_data.urlopen")

    with (
        patch("aw_client.client.SingleInstance"),
        client_factory as create_client,
        urlopen as open_url,
    ):
        assert aw_data._get_client() is None
        assert aw_data._fetch_category_rules() == []

    create_client.assert_not_called()
    open_url.assert_not_called()
    assert server not in caplog.text


def test_invalid_server_makes_activity_unavailable(monkeypatch):
    monkeypatch.setattr(aw_data, "AW_SERVER", "https://user:secret@aw.example.test/path")
    activity = aw_data.fetch_aw_activity(date.today(), date.today())
    assert not activity.available
    assert activity.top_apps == []
