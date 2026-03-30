"""Cross-process locking for OAuth2 token refresh.

Twitter uses rotating single-use refresh tokens (RFC 6749 §6).  If two
processes call the Twitter token endpoint with the same refresh token,
Twitter accepts only the first request and the second fails with 400,
leaving the account deauthenticated.

Serialising the critical section (check expiry → refresh network call →
atomic save → env reload) through an exclusive file lock prevents the race.
"""

import fcntl
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .oauth import OAuthManager
    from .tokens import TokenInfo


@contextmanager
def token_refresh_lock(env_path: Path):
    """Exclusive file lock serialising OAuth2 token refresh across processes.

    The lock file is placed alongside the .env file so that locks are
    scoped per-environment rather than per-machine.

    Usage::

        with token_refresh_lock(env_path):
            # Only one process executes this block at a time.
            ...
    """
    lock_path = env_path.parent / ".twitter-oauth-refresh.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def refresh_twitter_token_if_needed(
    oauth_manager: "OAuthManager",
    env_path: Path,
    *,
    buffer_seconds: int = 300,
) -> tuple["TokenInfo | None", str | None]:
    """Refresh Twitter OAuth2 tokens with cross-process locking.

    Acquires an exclusive lock, then re-reads .env to get the latest token
    state before deciding whether a network refresh is still needed.  This
    prevents two concurrent processes from both burning the single-use
    refresh token.

    After the lock is acquired:
    - If the token is still expired, performs a network refresh, saves the
      new tokens atomically to .env, and updates os.environ.
    - If another process already refreshed the token while we waited, returns
      the persisted (fresh) token without a network call.

    Args:
        oauth_manager: Configured OAuthManager for Twitter.
        env_path: Path to the .env file holding the tokens.
        buffer_seconds: Treat the token as expired this many seconds before
            its actual expiry (default 300 = 5 minutes).

    Returns:
        Tuple of (TokenInfo, error_message).  On success error_message is None.
    """
    from dotenv import dotenv_values

    from .token_storage import save_tokens_to_env
    from .tokens import TokenInfo

    with token_refresh_lock(env_path):
        # Re-read .env while holding the lock — another process may have
        # already refreshed since we first noticed the token was expired.
        env = dotenv_values(env_path)
        access_token = env.get("TWITTER_OAUTH2_ACCESS_TOKEN")
        refresh_token = env.get("TWITTER_OAUTH2_REFRESH_TOKEN")
        expires_at_str = env.get("TWITTER_OAUTH2_EXPIRES_AT")

        if not (access_token and refresh_token and expires_at_str):
            return None, "Missing token data in .env after acquiring refresh lock"

        expires_at = datetime.fromisoformat(expires_at_str)
        current_info = TokenInfo(
            token=access_token,
            expires_at=expires_at,
            refresh_token=refresh_token,
        )

        if not current_info.is_expired(buffer_seconds=buffer_seconds):
            # Another process already refreshed while we waited for the lock.
            return current_info, None

        # Token is still expired — this process is the designated refresher.
        new_info, error = oauth_manager.refresh_token(refresh_token)
        if error or not new_info:
            return None, error or "No token returned from refresh endpoint"

        if not new_info.expires_at:
            new_info = TokenInfo(
                token=new_info.token,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
                refresh_token=new_info.refresh_token,
                token_type=new_info.token_type,
            )

        tokens_to_save: dict[str, str] = {
            "TWITTER_OAUTH2_ACCESS_TOKEN": new_info.token,
        }
        if new_info.refresh_token:
            tokens_to_save["TWITTER_OAUTH2_REFRESH_TOKEN"] = new_info.refresh_token
        if new_info.expires_at:
            tokens_to_save["TWITTER_OAUTH2_EXPIRES_AT"] = new_info.expires_at.isoformat()

        save_tokens_to_env(
            tokens_to_save,
            env_path=env_path,
            comment="OAuth 2.0 tokens (auto-refreshed)",
        )

        # Update os.environ so the current process also picks up the new tokens.
        for key, value in tokens_to_save.items():
            os.environ[key] = value

        return new_info, None
