"""
Error handling and retry logic for cross-platform communication.

Provides retry decorators, exponential backoff, and common error
handling patterns for resilient communication systems.
"""

from .errors import (
    AuthenticationError,
    CommunicationError,
    NetworkError,
    RateLimitError,
)
from .retry import RetryConfig, RetryError, exponential_backoff, retry

__all__ = [
    "retry",
    "exponential_backoff",
    "RetryConfig",
    "RetryError",
    "CommunicationError",
    "RateLimitError",
    "AuthenticationError",
    "NetworkError",
]
