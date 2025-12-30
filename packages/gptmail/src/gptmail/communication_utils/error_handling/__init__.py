"""
Error handling and retry logic for cross-platform communication.

Provides retry decorators, exponential backoff, and common error
handling patterns for resilient communication systems.
"""

from .retry import retry, exponential_backoff, RetryConfig, RetryError
from .errors import (
    CommunicationError,
    RateLimitError,
    AuthenticationError,
    NetworkError,
)

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
