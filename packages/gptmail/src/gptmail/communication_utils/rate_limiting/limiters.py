"""Rate limiting utilities for managing API rate limits across platforms."""

import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict


@dataclass
class RateLimiter:
    """Simple token bucket rate limiter.

    Args:
        max_requests: Maximum number of requests allowed in window
        window: Time window in seconds
        name: Optional name for this limiter (for logging)

    Example:
        limiter = RateLimiter(max_requests=60, window=60)  # 1/sec
        if limiter.can_proceed():
            make_api_call()
    """

    max_requests: int
    window: float
    name: str | None = None

    # Internal state
    _requests: list[float] = field(default_factory=list, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def can_proceed(self) -> bool:
        """Check if a request can proceed within rate limits.

        Returns:
            True if request can proceed, False if rate limited
        """
        with self._lock:
            now = time.time()
            # Remove expired requests
            cutoff = now - self.window
            self._requests = [req_time for req_time in self._requests if req_time > cutoff]

            # Check if under limit
            if len(self._requests) < self.max_requests:
                self._requests.append(now)
                return True
            return False

    def wait_if_needed(self, max_wait: float = 60.0) -> bool:
        """Wait until rate limit allows request, up to max_wait seconds.

        Args:
            max_wait: Maximum seconds to wait (default 60)

        Returns:
            True if request can proceed, False if timed out
        """
        start = time.time()
        while time.time() - start < max_wait:
            if self.can_proceed():
                return True
            time.sleep(0.1)  # Short sleep between checks
        return False

    def time_until_ready(self) -> float:
        """Get seconds until next request can proceed.

        Returns:
            Seconds to wait (0 if ready now)
        """
        with self._lock:
            if len(self._requests) < self.max_requests:
                return 0.0

            now = time.time()
            cutoff = now - self.window
            # Find oldest request in current window
            valid_requests = [t for t in self._requests if t > cutoff]
            if not valid_requests:
                return 0.0

            oldest = min(valid_requests)
            return max(0.0, self.window - (now - oldest))

    @classmethod
    def for_platform(cls, platform: str) -> "RateLimiter":
        """Create rate limiter with platform-specific defaults.

        Args:
            platform: Platform name (email, twitter, discord)

        Returns:
            Configured RateLimiter for the platform

        Raises:
            ValueError: If platform not recognized
        """
        defaults: Dict[str, dict] = {
            "email": {"max_requests": 60, "window": 60},  # 1/sec
            "twitter": {"max_requests": 300, "window": 900},  # 20/min (300/15min)
            "discord": {"max_requests": 5, "window": 5},  # 1/sec
        }

        if platform.lower() not in defaults:
            raise ValueError(f"Unknown platform: {platform}. Use email, twitter, or discord.")

        config = defaults[platform.lower()]
        return cls(max_requests=config["max_requests"], window=config["window"], name=platform)


@dataclass
class GlobalRateLimiter:
    """Manages multiple rate limiters for different platforms.

    Example:
        manager = GlobalRateLimiter()
        if manager.can_proceed('twitter'):
            post_tweet()
    """

    _limiters: Dict[str, RateLimiter] = field(default_factory=dict)

    def get_limiter(self, platform: str) -> RateLimiter:
        """Get or create rate limiter for platform.

        Args:
            platform: Platform name

        Returns:
            RateLimiter for the platform
        """
        if platform not in self._limiters:
            self._limiters[platform] = RateLimiter.for_platform(platform)
        return self._limiters[platform]

    def can_proceed(self, platform: str) -> bool:
        """Check if request can proceed for platform.

        Args:
            platform: Platform name

        Returns:
            True if request can proceed
        """
        return self.get_limiter(platform).can_proceed()

    def wait_if_needed(self, platform: str, max_wait: float = 60.0) -> bool:
        """Wait for rate limit if needed.

        Args:
            platform: Platform name
            max_wait: Maximum seconds to wait

        Returns:
            True if request can proceed
        """
        return self.get_limiter(platform).wait_if_needed(max_wait)
