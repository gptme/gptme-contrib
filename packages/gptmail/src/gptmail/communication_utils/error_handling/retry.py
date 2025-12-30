"""
Retry logic with exponential backoff.

Provides decorators and utilities for retrying failed operations
with configurable backoff strategies and error handling.
"""

import time
import functools
from dataclasses import dataclass
from typing import Callable, Optional


class RetryError(Exception):
    """Raised when all retry attempts fail."""

    def __init__(self, message: str, attempts: int, last_error: Optional[Exception]):
        """
        Initialize retry error.

        Args:
            message: Error message
            attempts: Number of attempts made
            last_error: The last exception that occurred
        """
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""

    max_attempts: int = 3
    initial_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0
    jitter: bool = True
    retryable_exceptions: tuple = (Exception,)


def exponential_backoff(
    attempt: int,
    initial_delay: float = 1.0,
    exponential_base: float = 2.0,
    max_delay: float = 60.0,
    jitter: bool = True,
) -> float:
    """
    Calculate exponential backoff delay.

    Args:
        attempt: Current attempt number (0-indexed)
        initial_delay: Initial delay in seconds
        exponential_base: Base for exponential calculation
        max_delay: Maximum delay in seconds
        jitter: Whether to add random jitter

    Returns:
        Delay in seconds
    """
    delay = min(initial_delay * (exponential_base**attempt), max_delay)

    if jitter:
        import random

        # Add jitter (0-25% of delay)
        jitter_amount = delay * 0.25
        delay += random.uniform(0, jitter_amount)

    return delay


def retry(
    config: Optional[RetryConfig] = None,
    max_attempts: Optional[int] = None,
    initial_delay: Optional[float] = None,
    retryable_exceptions: Optional[tuple] = None,
):
    """
    Decorator for retrying failed operations with exponential backoff.

    Args:
        config: RetryConfig object (overrides other args)
        max_attempts: Maximum retry attempts
        initial_delay: Initial delay in seconds
        retryable_exceptions: Tuple of exceptions to retry on

    Usage:
        @retry(max_attempts=3, initial_delay=1.0)
        def my_function():
            # May fail and be retried
            pass

        @retry(config=RetryConfig(max_attempts=5))
        def another_function():
            pass
    """
    # Use config if provided, otherwise use individual args
    if config is None:
        config = RetryConfig(
            max_attempts=max_attempts or 3,
            initial_delay=initial_delay or 1.0,
            retryable_exceptions=retryable_exceptions or (Exception,),
        )

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None

            for attempt in range(config.max_attempts):
                try:
                    return func(*args, **kwargs)

                except config.retryable_exceptions as e:
                    last_error = e

                    # Don't sleep after last attempt
                    if attempt < config.max_attempts - 1:
                        delay = exponential_backoff(
                            attempt,
                            config.initial_delay,
                            config.exponential_base,
                            config.max_delay,
                            config.jitter,
                        )
                        time.sleep(delay)

            # All retries failed
            raise RetryError(
                f"Failed after {config.max_attempts} attempts",
                config.max_attempts,
                last_error,
            )

        return wrapper

    return decorator


def retry_with_rate_limit(
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    rate_limit_exceptions: Optional[tuple] = None,
):
    """
    Decorator for retrying with rate limit awareness.

    Automatically extracts retry_after from rate limit exceptions
    and uses that for backoff instead of exponential calculation.

    Args:
        max_attempts: Maximum retry attempts
        initial_delay: Initial delay if no retry_after provided
        rate_limit_exceptions: Exceptions that indicate rate limits

    Usage:
        from .errors import RateLimitError

        @retry_with_rate_limit(max_attempts=5)
        def api_call():
            # May raise RateLimitError
            pass
    """
    if rate_limit_exceptions is None:
        from .errors import RateLimitError

        rate_limit_exceptions = (RateLimitError,)

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None

            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)

                except rate_limit_exceptions as e:
                    last_error = e

                    # Don't sleep after last attempt
                    if attempt < max_attempts - 1:
                        # Use retry_after if available, otherwise exponential backoff
                        if hasattr(e, "retry_after") and e.retry_after:
                            delay = e.retry_after
                        else:
                            delay = exponential_backoff(attempt, initial_delay)

                        time.sleep(delay)

                except Exception:
                    # Non-rate-limit errors don't retry
                    raise

            # All retries failed
            raise RetryError(
                f"Rate limit persists after {max_attempts} attempts",
                max_attempts,
                last_error,
            )

        return wrapper

    return decorator
