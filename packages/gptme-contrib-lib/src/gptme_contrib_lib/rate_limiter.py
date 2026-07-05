"""Rate limiting for input sources.

Token bucket algorithm implementation for controlling request rates.
"""

import time
from dataclasses import dataclass
from typing import Dict


@dataclass
class RateLimitState:
    """State for token bucket rate limiter.

    Attributes:
        tokens: Current number of available tokens (float for precision)
        last_update: Timestamp of last token refill (seconds since epoch)
        max_tokens: Maximum token capacity
        refill_rate: Rate at which tokens refill (tokens per second)
    """

    tokens: float
    last_update: float
    max_tokens: int
    refill_rate: float  # tokens per second

    def refill(self) -> None:
        """Refill tokens based on elapsed time since last update.

        Updates the token count by adding tokens earned during the elapsed time,
        capped at max_tokens. Also updates the last_update timestamp to now.
        """
        now = time.time()
        elapsed = now - self.last_update

        # Add tokens based on elapsed time
        tokens_to_add = elapsed * self.refill_rate
        self.tokens = min(self.max_tokens, self.tokens + tokens_to_add)
        self.last_update = now

    def consume(self, count: int = 1) -> bool:
        """Consume tokens if available.

        Args:
            count: Number of tokens to consume

        Returns:
            True if tokens were consumed, False if insufficient
        """
        self.refill()

        if self.tokens >= count:
            self.tokens -= count
            return True
        return False

    def get_wait_time(self, count: int = 1) -> float:
        """Get time to wait until tokens available.

        Args:
            count: Number of tokens needed

        Returns:
            Seconds to wait (0 if tokens available)
        """
        self.refill()

        if self.tokens >= count:
            return 0.0

        tokens_needed = count - self.tokens
        return tokens_needed / self.refill_rate


class RateLimiter:
    """Rate limiter using token bucket algorithm.

    Supports both per-minute and per-hour limits with separate buckets.

    Attributes:
        max_per_minute: Maximum requests allowed per minute
        max_per_hour: Maximum requests allowed per hour
        minute_buckets: Dict mapping source names to per-minute rate limit states
        hour_buckets: Dict mapping source names to per-hour rate limit states
    """

    def __init__(
        self,
        max_per_minute: int = 60,
        max_per_hour: int = 1000,
    ):
        """Initialize rate limiter.

        Args:
            max_per_minute: Maximum requests per minute
            max_per_hour: Maximum requests per hour
        """
        self.max_per_minute = max_per_minute
        self.max_per_hour = max_per_hour

        # Per-source rate limit states
        self.minute_buckets: Dict[str, RateLimitState] = {}
        self.hour_buckets: Dict[str, RateLimitState] = {}

    def _get_or_create_minute_bucket(self, source_name: str) -> RateLimitState:
        """Get or create per-minute bucket for source.

        Args:
            source_name: Name of the source

        Returns:
            RateLimitState for per-minute limiting
        """
        if source_name not in self.minute_buckets:
            now = time.time()
            self.minute_buckets[source_name] = RateLimitState(
                tokens=self.max_per_minute,
                last_update=now,
                max_tokens=self.max_per_minute,
                refill_rate=self.max_per_minute / 60.0,  # tokens per second
            )
        return self.minute_buckets[source_name]

    def _get_or_create_hour_bucket(self, source_name: str) -> RateLimitState:
        """Get or create per-hour bucket for source.

        Args:
            source_name: Name of the source

        Returns:
            RateLimitState for per-hour limiting
        """
        if source_name not in self.hour_buckets:
            now = time.time()
            self.hour_buckets[source_name] = RateLimitState(
                tokens=self.max_per_hour,
                last_update=now,
                max_tokens=self.max_per_hour,
                refill_rate=self.max_per_hour / 3600.0,  # tokens per second
            )
        return self.hour_buckets[source_name]

    def check_limit(self, source_name: str, count: int = 1) -> bool:
        """Check if request would exceed rate limit.

        Args:
            source_name: Name of the source
            count: Number of tokens needed

        Returns:
            True if request is allowed, False if rate limited
        """
        minute_bucket = self._get_or_create_minute_bucket(source_name)
        hour_bucket = self._get_or_create_hour_bucket(source_name)

        # Refill both buckets
        minute_bucket.refill()
        hour_bucket.refill()

        # Check if both buckets have enough tokens
        return minute_bucket.tokens >= count and hour_bucket.tokens >= count

    def consume(self, source_name: str, count: int = 1) -> bool:
        """Consume tokens if available.

        Args:
            source_name: Name of the source
            count: Number of tokens to consume

        Returns:
            True if tokens were consumed, False if rate limited
        """
        minute_bucket = self._get_or_create_minute_bucket(source_name)
        hour_bucket = self._get_or_create_hour_bucket(source_name)

        # Try to consume from both buckets
        minute_ok = minute_bucket.consume(count)
        hour_ok = hour_bucket.consume(count)

        # If either failed, refund and return False
        if not (minute_ok and hour_ok):
            # Refund consumed tokens
            if minute_ok:
                minute_bucket.tokens += count
            if hour_ok:
                hour_bucket.tokens += count
            return False

        return True

    def get_wait_time(self, source_name: str, count: int = 1) -> float:
        """Get time to wait until request is allowed.

        Args:
            source_name: Name of the source
            count: Number of tokens needed

        Returns:
            Seconds to wait (0 if request allowed now)
        """
        minute_bucket = self._get_or_create_minute_bucket(source_name)
        hour_bucket = self._get_or_create_hour_bucket(source_name)

        minute_wait = minute_bucket.get_wait_time(count)
        hour_wait = hour_bucket.get_wait_time(count)

        # Return the longer wait time
        return max(minute_wait, hour_wait)

    def reset_source(self, source_name: str) -> None:
        """Reset rate limit state for a source.

        Args:
            source_name: Name of the source
        """
        self.minute_buckets.pop(source_name, None)
        self.hour_buckets.pop(source_name, None)

    def get_status(self, source_name: str) -> Dict:
        """Get current rate limit status for a source.

        Args:
            source_name: Name of the source

        Returns:
            Dictionary with rate limit status
        """
        minute_bucket = self._get_or_create_minute_bucket(source_name)
        hour_bucket = self._get_or_create_hour_bucket(source_name)

        minute_bucket.refill()
        hour_bucket.refill()

        return {
            "source_name": source_name,
            "per_minute": {
                "available_tokens": int(minute_bucket.tokens),
                "max_tokens": minute_bucket.max_tokens,
                "refill_rate": minute_bucket.refill_rate,
                "usage_percent": (
                    (1 - minute_bucket.tokens / minute_bucket.max_tokens) * 100
                ),
            },
            "per_hour": {
                "available_tokens": int(hour_bucket.tokens),
                "max_tokens": hour_bucket.max_tokens,
                "refill_rate": hour_bucket.refill_rate,
                "usage_percent": (
                    (1 - hour_bucket.tokens / hour_bucket.max_tokens) * 100
                ),
            },
        }


class MultiSourceRateLimiter:
    """Rate limiter managing multiple sources with different limits.

    Attributes:
        limiters: Dict mapping source names to their RateLimiter instances
    """

    def __init__(self):
        """Initialize multi-source rate limiter with empty limiters dict."""
        self.limiters: Dict[str, RateLimiter] = {}

    def register_source(
        self,
        source_name: str,
        max_per_minute: int = 60,
        max_per_hour: int = 1000,
    ) -> None:
        """Register a source with specific rate limits.

        Args:
            source_name: Name of the source
            max_per_minute: Maximum requests per minute
            max_per_hour: Maximum requests per hour
        """
        self.limiters[source_name] = RateLimiter(
            max_per_minute=max_per_minute,
            max_per_hour=max_per_hour,
        )

    def check_limit(self, source_name: str, count: int = 1) -> bool:
        """Check if request would exceed rate limit.

        Args:
            source_name: Name of the source
            count: Number of tokens needed

        Returns:
            True if request is allowed, False if rate limited
        """
        if source_name not in self.limiters:
            # No limit configured, allow request
            return True

        return self.limiters[source_name].check_limit(source_name, count)

    def consume(self, source_name: str, count: int = 1) -> bool:
        """Consume tokens if available.

        Args:
            source_name: Name of the source
            count: Number of tokens to consume

        Returns:
            True if tokens were consumed, False if rate limited
        """
        if source_name not in self.limiters:
            # No limit configured, allow request
            return True

        return self.limiters[source_name].consume(source_name, count)

    def get_wait_time(self, source_name: str, count: int = 1) -> float:
        """Get time to wait until request is allowed.

        Args:
            source_name: Name of the source
            count: Number of tokens needed

        Returns:
            Seconds to wait (0 if request allowed now)
        """
        if source_name not in self.limiters:
            return 0.0

        return self.limiters[source_name].get_wait_time(source_name, count)

    def get_status(self, source_name: str) -> Dict | None:
        """Get current rate limit status for a source.

        Args:
            source_name: Name of the source

        Returns:
            Dictionary with rate limit status, or None if not registered
        """
        if source_name not in self.limiters:
            return None

        return self.limiters[source_name].get_status(source_name)

    def get_all_status(self) -> Dict[str, Dict]:
        """Get rate limit status for all registered sources.

        Returns:
            Dictionary mapping source names to their status
        """
        return {
            source_name: limiter.get_status(source_name)
            for source_name, limiter in self.limiters.items()
        }
