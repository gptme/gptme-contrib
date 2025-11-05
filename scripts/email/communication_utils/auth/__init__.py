"""
Authentication utilities for cross-platform communication.

Provides OAuth flows, token management, and credential handling
for email, Twitter, Discord, and other platforms.
"""

from .callback_server import CallbackServer, run_oauth_callback
from .oauth import OAuthManager
from .tokens import TokenManager

__all__ = ["CallbackServer", "OAuthManager", "TokenManager", "run_oauth_callback"]
