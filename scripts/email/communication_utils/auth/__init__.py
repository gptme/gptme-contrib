"""
Authentication utilities for cross-platform communication.

Provides OAuth flows, token management, and credential handling
for email, Twitter, Discord, and other platforms.
"""

from .oauth import OAuthManager
from .tokens import TokenManager

__all__ = ["OAuthManager", "TokenManager"]
