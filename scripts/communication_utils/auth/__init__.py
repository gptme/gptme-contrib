"""
Authentication utilities for cross-platform communication.

Provides OAuth flows, token management, and credential handling
for email, Twitter, Discord, and other platforms.
"""

from .callback_server import CallbackServer, run_oauth_callback
from .oauth import OAuthManager
from .token_storage import save_token_to_env
from .tokens import TokenManager

__all__ = [
    "CallbackServer",
    "OAuthManager",
    "TokenManager",
    "run_oauth_callback",
    "save_token_to_env",
]
