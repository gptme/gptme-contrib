"""
Token management for platform authentication.

Handles token validation, expiry checking, and credential retrieval
from environment variables with secure defaults.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class TokenInfo:
    """Information about an authentication token."""

    token: str
    expires_at: Optional[datetime] = None
    refresh_token: Optional[str] = None
    token_type: str = "Bearer"

    def is_expired(self, buffer_seconds: int = 300) -> bool:
        """
        Check if token is expired or will expire soon.

        Args:
            buffer_seconds: Consider token expired if within this many seconds of expiry
        """
        if not self.expires_at:
            return False  # No expiry info means we assume valid

        buffer = timedelta(seconds=buffer_seconds)
        return datetime.now() >= (self.expires_at - buffer)

    def is_valid(self) -> bool:
        """Check if token is valid (exists and not expired)."""
        return bool(self.token) and not self.is_expired()


class TokenManager:
    """
    Manages authentication tokens across platforms.

    Retrieves credentials from environment variables and provides
    token validation and expiry checking.
    """

    # Environment variable patterns for different platforms
    ENV_PATTERNS = {
        "email": "GMAIL_APP_PASSWORD",
        "twitter": "TWITTER_BEARER_TOKEN",
        "discord": "DISCORD_TOKEN",
        "github": "GITHUB_TOKEN",
    }

    @classmethod
    def get_token(cls, platform: str) -> Optional[str]:
        """
        Retrieve token for the specified platform from environment.

        Args:
            platform: Platform name (email, twitter, discord, github)

        Returns:
            Token string or None if not found
        """
        env_var = cls.ENV_PATTERNS.get(platform.lower())
        if not env_var:
            raise ValueError(f"Unknown platform: {platform}")

        return os.getenv(env_var)

    @classmethod
    def get_token_info(
        cls, platform: str, expires_at: Optional[datetime] = None
    ) -> Optional[TokenInfo]:
        """
        Get TokenInfo for the specified platform.

        Args:
            platform: Platform name
            expires_at: Optional expiry datetime

        Returns:
            TokenInfo object or None if token not found
        """
        token = cls.get_token(platform)
        if not token:
            return None

        return TokenInfo(token=token, expires_at=expires_at)

    @classmethod
    def validate_token(cls, platform: str) -> bool:
        """
        Validate that a token exists for the platform.

        Args:
            platform: Platform name

        Returns:
            True if token exists and is non-empty
        """
        token = cls.get_token(platform)
        return bool(token)

    @staticmethod
    def create_bearer_header(token: str) -> dict[str, str]:
        """
        Create Authorization header with Bearer token.

        Args:
            token: The authentication token

        Returns:
            Dictionary with Authorization header
        """
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def create_basic_auth_header(username: str, password: str) -> dict[str, str]:
        """
        Create Authorization header with Basic auth.

        Args:
            username: Username for authentication
            password: Password for authentication

        Returns:
            Dictionary with Authorization header
        """
        import base64

        credentials = f"{username}:{password}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return {"Authorization": f"Basic {encoded}"}
