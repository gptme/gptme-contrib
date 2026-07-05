"""Tests for cross-process OAuth2 token refresh serialization.

The key invariant: when two callers race to refresh a rotating single-use
refresh token, exactly one network call is made.  The second caller must
reuse the persisted state written by the first.
"""

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock


from gptmail.communication_utils.auth.refresh import (
    refresh_twitter_token_if_needed,
    token_refresh_lock,
)
from gptmail.communication_utils.auth.tokens import TokenInfo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXPIRED = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
_FUTURE = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()


def _write_env(path: Path, *, access: str, refresh: str, expires: str) -> None:
    path.write_text(
        f"TWITTER_OAUTH2_ACCESS_TOKEN={access}\n"
        f"TWITTER_OAUTH2_REFRESH_TOKEN={refresh}\n"
        f"TWITTER_OAUTH2_EXPIRES_AT={expires}\n"
    )


def _make_oauth_manager(
    new_access: str = "new-access",
    new_refresh: str = "new-refresh",
    *,
    call_counter: list[int] | None = None,
) -> MagicMock:
    """Return a mock OAuthManager whose refresh_token() records calls."""

    def _refresh(token: str) -> tuple[TokenInfo | None, str | None]:
        if call_counter is not None:
            call_counter.append(1)
        return (
            TokenInfo(
                token=new_access,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
                refresh_token=new_refresh,
            ),
            None,
        )

    manager = MagicMock()
    manager.refresh_token.side_effect = _refresh
    return manager


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_refresh_expired_token(tmp_path: Path) -> None:
    """Expired token triggers one network refresh and persists new tokens."""
    env = tmp_path / ".env"
    _write_env(env, access="old-access", refresh="old-refresh", expires=_EXPIRED)

    calls: list[int] = []
    manager = _make_oauth_manager("new-access", "new-refresh", call_counter=calls)

    token_info, error = refresh_twitter_token_if_needed(manager, env)

    assert error is None
    assert token_info is not None
    assert token_info.token == "new-access"
    assert token_info.refresh_token == "new-refresh"
    assert len(calls) == 1

    # Verify persisted to .env
    content = env.read_text()
    assert "TWITTER_OAUTH2_ACCESS_TOKEN=new-access" in content
    assert "TWITTER_OAUTH2_REFRESH_TOKEN=new-refresh" in content


def test_valid_token_skips_network_call(tmp_path: Path) -> None:
    """Non-expired token returns persisted state without a network call."""
    env = tmp_path / ".env"
    _write_env(env, access="good-access", refresh="good-refresh", expires=_FUTURE)

    calls: list[int] = []
    manager = _make_oauth_manager(call_counter=calls)

    token_info, error = refresh_twitter_token_if_needed(manager, env)

    assert error is None
    assert token_info is not None
    assert token_info.token == "good-access"
    assert len(calls) == 0  # no network call


def test_missing_env_tokens_returns_error(tmp_path: Path) -> None:
    """Missing tokens in .env return an error without a network call."""
    env = tmp_path / ".env"
    env.write_text("# empty\n")

    manager = _make_oauth_manager()
    token_info, error = refresh_twitter_token_if_needed(manager, env)

    assert token_info is None
    assert error is not None
    manager.refresh_token.assert_not_called()


def test_refresh_error_propagates(tmp_path: Path) -> None:
    """Network error from refresh_token() is returned as an error string."""
    env = tmp_path / ".env"
    _write_env(env, access="old-access", refresh="old-refresh", expires=_EXPIRED)

    manager = MagicMock()
    manager.refresh_token.return_value = (None, "Token refresh failed: 400 Bad Request")

    token_info, error = refresh_twitter_token_if_needed(manager, env)

    assert token_info is None
    assert "400 Bad Request" in (error or "")


def test_concurrent_refresh_only_one_network_call(tmp_path: Path) -> None:
    """Two threads racing to refresh cause exactly one network call.

    This is the core invariant for rotating single-use refresh tokens.
    The second thread acquires the lock after the first has already written
    the new tokens and finds the token is no longer expired.
    """
    env = tmp_path / ".env"
    _write_env(env, access="old-access", refresh="old-refresh", expires=_EXPIRED)

    calls: list[int] = []
    results: list[tuple[TokenInfo | None, str | None]] = []
    lock = threading.Lock()

    def _worker() -> None:
        manager = _make_oauth_manager("new-access", "new-refresh", call_counter=calls)
        result = refresh_twitter_token_if_needed(manager, env)
        with lock:
            results.append(result)

    t1 = threading.Thread(target=_worker)
    t2 = threading.Thread(target=_worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Both callers got a valid token
    assert len(results) == 2
    for token_info, error in results:
        assert error is None
        assert token_info is not None
        assert token_info.token == "new-access"

    # Exactly one network refresh occurred
    assert len(calls) == 1, f"Expected 1 network call, got {len(calls)}"


def test_token_refresh_lock_is_exclusive(tmp_path: Path) -> None:
    """Verify the lock actually blocks: second entrant waits for first."""
    env = tmp_path / ".env"
    env.touch()

    order: list[str] = []
    lock = threading.Lock()

    def _first() -> None:
        with token_refresh_lock(env):
            with lock:
                order.append("first-in")
            # Hold the lock while second thread tries to enter
            threading.Event().wait(timeout=0.05)
            with lock:
                order.append("first-out")

    def _second() -> None:
        # Give first thread time to acquire the lock
        threading.Event().wait(timeout=0.02)
        with token_refresh_lock(env):
            with lock:
                order.append("second-in")

    t1 = threading.Thread(target=_first)
    t2 = threading.Thread(target=_second)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # second-in must come after first-out (i.e., lock was actually exclusive)
    assert order.index("first-out") < order.index(
        "second-in"
    ), f"Lock was not exclusive; order: {order}"
