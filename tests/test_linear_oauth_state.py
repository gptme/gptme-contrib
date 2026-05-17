"""Regression tests for Linear OAuth state validation."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

SCRIPT_DIR = Path(__file__).parent.parent / "scripts" / "linear"
sys.path.insert(0, str(SCRIPT_DIR))

LINEAR_ACTIVITY_PATH = SCRIPT_DIR / "linear-activity.py"
LINEAR_WEBHOOK_PATH = SCRIPT_DIR / "linear-webhook-server.py"


class DummyResponse:
    """Minimal httpx-style response object for auth tests."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: dict[str, object] | None = None,
        text: str = "ok",
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {"access_token": "test-token", "expires_in": 3600}
        self.text = text

    def json(self) -> dict[str, object]:
        return self._payload


def load_script_module(name: str, path: Path):
    """Import a script module directly from its filesystem path."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure_state_file(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    state_file: Path,
    *,
    expected_state: str | None = None,
) -> None:
    """Route OAuth state storage to a temp file for the test."""
    monkeypatch.setattr(module, "OAUTH_STATE_FILE", state_file, raising=False)

    oauth_state = sys.modules.get("oauth_state")
    if oauth_state is not None:
        monkeypatch.setattr(oauth_state, "OAUTH_STATE_FILE", state_file)
        if expected_state is not None:
            oauth_state.save_pending_oauth_state(expected_state)
        return

    if expected_state is not None:
        state_file.write_text(expected_state)


@pytest.fixture
def linear_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set the required Linear webhook/CLI environment."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setenv("AGENT_NAME", "bob")
    monkeypatch.setenv("AGENT_WORKSPACE", str(workspace))
    monkeypatch.setenv("DEFAULT_BRANCH", "master")
    monkeypatch.setenv("LINEAR_CLIENT_ID", "client-id")
    monkeypatch.setenv("LINEAR_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("LINEAR_CALLBACK_URL", "https://example.com/oauth/callback")
    return tmp_path


def test_do_auth_rejects_mismatched_state_before_token_exchange(
    linear_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI auth must reject a callback URL whose state does not match."""
    activity = load_script_module("linear_activity_under_test", LINEAR_ACTIVITY_PATH)
    token_file = linear_env / "tokens.json"
    state_file = linear_env / "oauth-state.json"
    configure_state_file(monkeypatch, activity, state_file)
    monkeypatch.setattr(activity, "TOKENS_FILE", token_file)

    printed: list[str] = []
    token_exchange_called = False

    def fake_print(*args: object, **kwargs: object) -> None:
        printed.append(" ".join(str(arg) for arg in args))

    def fake_input(_prompt: str) -> str:
        auth_url = next(
            line.strip()
            for line in printed
            if "https://linear.app/oauth/authorize" in line
        )
        expected_state = parse_qs(urlparse(auth_url).query)["state"][0]
        return (
            "https://example.com/oauth/callback?"
            f"code=test-code&state={expected_state}-wrong"
        )

    def fake_post(*args: object, **kwargs: object) -> DummyResponse:
        nonlocal token_exchange_called
        token_exchange_called = True
        return DummyResponse()

    monkeypatch.setattr("builtins.print", fake_print)
    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(activity.httpx, "post", fake_post)

    with pytest.raises(activity.AuthenticationError, match="state"):
        activity.do_auth()

    assert not token_exchange_called
    assert not token_file.exists()


def test_oauth_callback_rejects_mismatched_state_before_token_exchange(
    linear_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Webhook callback must reject a mismatched OAuth state."""
    webhook = load_script_module("linear_webhook_under_test", LINEAR_WEBHOOK_PATH)
    token_file = linear_env / "tokens.json"
    state_file = linear_env / "oauth-state.json"
    configure_state_file(
        monkeypatch,
        webhook,
        state_file,
        expected_state="expected-state",
    )
    monkeypatch.setattr(webhook, "TOKENS_FILE", token_file)

    token_exchange_called = False

    def fake_post(*args: object, **kwargs: object) -> DummyResponse:
        nonlocal token_exchange_called
        token_exchange_called = True
        return DummyResponse()

    monkeypatch.setattr(webhook.httpx, "post", fake_post)

    response = webhook.app.test_client().get(
        "/oauth/callback?code=test-code&state=wrong-state"
    )

    assert response.status_code == 400
    assert "state" in response.get_data(as_text=True).lower()
    assert not token_exchange_called
    assert not token_file.exists()
