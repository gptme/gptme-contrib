"""
Common error classes for communication systems.

Provides consistent error hierarchy for handling failures
across email, Twitter, Discord, and other platforms.
"""


class CommunicationError(Exception):
    """Base exception for all communication errors."""

    def __init__(self, message: str, platform: str, details: dict | None = None):
        """
        Initialize communication error.

        Args:
            message: Error message
            platform: Platform where error occurred
            details: Optional error details
        """
        super().__init__(message)
        self.platform = platform
        self.details = details or {}


class RateLimitError(CommunicationError):
    """Raised when rate limit is exceeded."""

    def __init__(
        self,
        platform: str,
        retry_after: int | None = None,
        message: str = "Rate limit exceeded",
    ):
        """
        Initialize rate limit error.

        Args:
            platform: Platform where rate limit occurred
            retry_after: Seconds until rate limit resets
            message: Error message
        """
        super().__init__(message, platform, {"retry_after": retry_after})
        self.retry_after = retry_after


class AuthenticationError(CommunicationError):
    """Raised when authentication fails."""

    def __init__(self, platform: str, message: str = "Authentication failed"):
        """
        Initialize authentication error.

        Args:
            platform: Platform where auth failed
            message: Error message
        """
        super().__init__(message, platform)


class NetworkError(CommunicationError):
    """Raised when network request fails."""

    def __init__(
        self,
        platform: str,
        status_code: int | None = None,
        message: str = "Network error",
    ):
        """
        Initialize network error.

        Args:
            platform: Platform where network error occurred
            status_code: HTTP status code if applicable
            message: Error message
        """
        super().__init__(message, platform, {"status_code": status_code})
        self.status_code = status_code


class ConfigurationError(CommunicationError):
    """Raised when configuration is invalid or missing."""

    def __init__(self, platform: str, message: str = "Configuration error"):
        """
        Initialize configuration error.

        Args:
            platform: Platform with configuration issue
            message: Error message
        """
        super().__init__(message, platform)


class MessageError(CommunicationError):
    """Raised when message processing fails."""

    def __init__(
        self,
        platform: str,
        message_id: str | None = None,
        message: str = "Message processing failed",
    ):
        """
        Initialize message error.

        Args:
            platform: Platform where message error occurred
            message_id: Optional message identifier
            message: Error message
        """
        super().__init__(message, platform, {"message_id": message_id})
        self.message_id = message_id
