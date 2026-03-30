"""
Authentication utilities for cross-platform communication.

Provides OAuth flows, token management, and credential handling
for email, Twitter, Discord, and other platforms.
"""

from .oauth import OAuthManager
from .refresh import refresh_twitter_token_if_needed, token_refresh_lock
from .token_storage import save_token_to_env, save_tokens_to_env
from .tokens import TokenInfo, TokenManager

# Flask-dependent OAuth callback server (requires gptmail[oauth])
try:
    from .callback_server import CallbackServer, run_oauth_callback

    __all__ = [
        "CallbackServer",
        "OAuthManager",
        "TokenInfo",
        "TokenManager",
        "refresh_twitter_token_if_needed",
        "run_oauth_callback",
        "save_token_to_env",
        "save_tokens_to_env",
        "token_refresh_lock",
    ]
except ImportError:
    __all__ = [
        "OAuthManager",
        "TokenInfo",
        "TokenManager",
        "refresh_twitter_token_if_needed",
        "save_token_to_env",
        "save_tokens_to_env",
        "token_refresh_lock",
    ]
