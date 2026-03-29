"""Tests for OAuthManager token endpoint auth modes."""

from requests.auth import HTTPBasicAuth

from gptmail.communication_utils.auth.oauth import OAuthManager


class _FakeResponse:
    def __init__(self, payload: dict[str, str | int]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, str | int]:
        return self._payload


def test_twitter_refresh_uses_basic_auth(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, data: dict[str, str], auth: object, timeout: int) -> _FakeResponse:
        captured["url"] = url
        captured["data"] = data
        captured["auth"] = auth
        captured["timeout"] = timeout
        return _FakeResponse(
            {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 7200,
            }
        )

    monkeypatch.setattr("gptmail.communication_utils.auth.oauth.requests.post", fake_post)

    manager = OAuthManager.for_twitter("client-id", "client-secret")
    token_info, error = manager.refresh_token("old-refresh")

    assert error is None
    assert token_info is not None
    assert token_info.token == "new-access"
    assert token_info.refresh_token == "new-refresh"
    assert captured["url"] == "https://api.twitter.com/2/oauth2/token"
    assert captured["timeout"] == 30
    assert captured["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "old-refresh",
        "client_id": "client-id",
    }

    auth = captured["auth"]
    assert isinstance(auth, HTTPBasicAuth)
    assert auth.username == "client-id"
    assert auth.password == "client-secret"


def test_twitter_code_exchange_uses_basic_auth(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, data: dict[str, str], auth: object, timeout: int) -> _FakeResponse:
        captured["url"] = url
        captured["data"] = data
        captured["auth"] = auth
        captured["timeout"] = timeout
        return _FakeResponse({"access_token": "new-access", "expires_in": 3600})

    monkeypatch.setattr("gptmail.communication_utils.auth.oauth.requests.post", fake_post)

    manager = OAuthManager.for_twitter("client-id", "client-secret")
    token_info, error = manager.exchange_code_for_token("auth-code")

    assert error is None
    assert token_info is not None
    assert token_info.token == "new-access"
    assert captured["data"] == {
        "grant_type": "authorization_code",
        "code": "auth-code",
        "redirect_uri": "http://localhost:8080/callback",
        "client_id": "client-id",
    }

    auth = captured["auth"]
    assert isinstance(auth, HTTPBasicAuth)
    assert auth.username == "client-id"
    assert auth.password == "client-secret"


def test_github_refresh_keeps_client_secret_in_body(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, data: dict[str, str], auth: object, timeout: int) -> _FakeResponse:
        captured["url"] = url
        captured["data"] = data
        captured["auth"] = auth
        captured["timeout"] = timeout
        return _FakeResponse({"access_token": "gh-access"})

    monkeypatch.setattr("gptmail.communication_utils.auth.oauth.requests.post", fake_post)

    manager = OAuthManager.for_github("client-id", "client-secret")
    token_info, error = manager.refresh_token("refresh-token")

    assert error is None
    assert token_info is not None
    assert token_info.token == "gh-access"
    assert captured["auth"] is None
    assert captured["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-token",
        "client_id": "client-id",
        "client_secret": "client-secret",
    }
