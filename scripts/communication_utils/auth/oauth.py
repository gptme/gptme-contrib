"""
OAuth 2.0 flow helpers for platform authentication.

Provides common OAuth patterns for authorization, token exchange,
and token refresh across different platforms.
"""

import base64
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qs
import requests
from .tokens import TokenInfo


@dataclass
class OAuthConfig:
    """Configuration for OAuth 2.0 flow."""

    client_id: str
    client_secret: str
    redirect_uri: str
    auth_url: str  # Authorization endpoint
    token_url: str  # Token exchange endpoint
    scopes: list[str]

    def get_authorization_url(self, state: Optional[str] = None) -> str:
        """
        Generate OAuth authorization URL.

        Args:
            state: Optional state parameter for CSRF protection

        Returns:
            Authorization URL for user to visit
        """
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
        }

        if state:
            params["state"] = state

        return f"{self.auth_url}?{urlencode(params)}"


class OAuthManager:
    """
    Manages OAuth 2.0 flows across platforms.

    Handles authorization, token exchange, and token refresh
    with platform-agnostic patterns.
    """

    def __init__(self, config: OAuthConfig):
        """
        Initialize OAuth manager.

        Args:
            config: OAuth configuration for the platform
        """
        self.config = config

    def get_authorization_url(self, state: Optional[str] = None) -> str:
        """
        Get authorization URL for user to visit.

        Args:
            state: Optional state parameter for CSRF protection

        Returns:
            Authorization URL
        """
        return self.config.get_authorization_url(state)

    def _get_auth_headers(self) -> dict[str, str]:
        """
        Get authorization headers for token requests.

        Returns:
            Headers dict with Basic auth for confidential clients.
        """
        credentials = base64.b64encode(
            f"{self.config.client_id}:{self.config.client_secret}".encode()
        ).decode()
        return {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    def exchange_code_for_token(
        self, authorization_code: str
    ) -> tuple[Optional[TokenInfo], Optional[str]]:
        """
        Exchange authorization code for access token.

        Args:
            authorization_code: Authorization code from callback

        Returns:
            Tuple of (TokenInfo, error_message)
        """
        data = {
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": self.config.redirect_uri,
        }

        try:
            response = requests.post(
                self.config.token_url,
                headers=self._get_auth_headers(),
                data=data,
                timeout=30,
            )
            response.raise_for_status()

            token_data = response.json()
            return self._parse_token_response(token_data), None

        except requests.RequestException as e:
            return None, f"Token exchange failed: {e}"

    def refresh_token(
        self, refresh_token: str
    ) -> tuple[Optional[TokenInfo], Optional[str]]:
        """
        Refresh an expired access token.

        Args:
            refresh_token: Refresh token from previous authorization

        Returns:
            Tuple of (TokenInfo, error_message)
        """
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }

        try:
            response = requests.post(
                self.config.token_url,
                headers=self._get_auth_headers(),
                data=data,
                timeout=30,
            )
            response.raise_for_status()

            token_data = response.json()
            return self._parse_token_response(token_data), None

        except requests.RequestException as e:
            return None, f"Token refresh failed: {e}"

    @staticmethod
    def parse_callback_url(callback_url: str) -> tuple[Optional[str], Optional[str]]:
        """
        Parse authorization callback URL to extract code and state.

        Args:
            callback_url: Full callback URL with query parameters

        Returns:
            Tuple of (authorization_code, state)
        """
        parsed = urlparse(callback_url)
        params = parse_qs(parsed.query)

        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]

        return code, state

    def _parse_token_response(self, response_data: dict) -> TokenInfo:
        """
        Parse token response into TokenInfo object.

        Args:
            response_data: JSON response from token endpoint

        Returns:
            TokenInfo with token details
        """
        from datetime import datetime, timedelta

        access_token = response_data["access_token"]
        token_type = response_data.get("token_type", "Bearer")
        refresh_token = response_data.get("refresh_token")
        expires_in = response_data.get("expires_in")

        expires_at = None
        if expires_in:
            expires_at = datetime.now() + timedelta(seconds=int(expires_in))

        return TokenInfo(
            token=access_token,
            expires_at=expires_at,
            refresh_token=refresh_token,
            token_type=token_type,
        )

    @classmethod
    def for_twitter(cls, client_id: str, client_secret: str) -> "OAuthManager":
        """
        Create OAuth manager configured for Twitter API v2.

        Args:
            client_id: Twitter OAuth 2.0 client ID
            client_secret: Twitter OAuth 2.0 client secret

        Returns:
            Configured OAuthManager instance
        """
        config = OAuthConfig(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri="http://localhost:8080/callback",
            auth_url="https://twitter.com/i/oauth2/authorize",
            token_url="https://api.twitter.com/2/oauth2/token",
            scopes=["tweet.read", "tweet.write", "users.read", "offline.access"],
        )
        return cls(config)

    @classmethod
    def for_github(cls, client_id: str, client_secret: str) -> "OAuthManager":
        """
        Create OAuth manager configured for GitHub.

        Args:
            client_id: GitHub OAuth app client ID
            client_secret: GitHub OAuth app client secret

        Returns:
            Configured OAuthManager instance
        """
        config = OAuthConfig(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri="http://localhost:8080/callback",
            auth_url="https://github.com/login/oauth/authorize",
            token_url="https://github.com/login/oauth/access_token",
            scopes=["repo", "user"],
        )
        return cls(config)
