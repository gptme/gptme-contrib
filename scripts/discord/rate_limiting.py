"""Per-user rate limiting for Discord bot."""

from threading import Lock
from typing import Dict

import discord
from communication_utils.rate_limiting.limiters import RateLimiter


class PerUserRateLimiter:
    """Manages per-user rate limiters for Discord.

    Creates and manages separate RateLimiter instances for each user,
    with support for DM-specific rate adjustment.
    """

    def __init__(self, base_rate: float, dm_multiplier: float = 0.5):
        """Initialize per-user rate limiter.

        Args:
            base_rate: Base rate limit in seconds between messages
            dm_multiplier: Multiplier for DM channels (default 0.5 = faster rate for DMs)
        """
        self.base_rate = base_rate
        self.dm_multiplier = dm_multiplier
        self._limiters: Dict[int, RateLimiter] = {}
        self._lock = Lock()

    def get_limiter(self, user_id: int, is_dm: bool = False) -> RateLimiter:
        """Get or create rate limiter for user.

        Args:
            user_id: Discord user ID
            is_dm: Whether this is a DM channel

        Returns:
            RateLimiter instance for this user
        """
        with self._lock:
            if user_id not in self._limiters:
                rate = self.base_rate * (self.dm_multiplier if is_dm else 1.0)
                # Convert to max_requests/window (e.g., rate=1.0 -> 1 req/sec)
                max_requests = 1
                window = 1.0 / rate if rate > 0 else 1.0
                self._limiters[user_id] = RateLimiter(
                    max_requests=max_requests, window=window, name=f"user_{user_id}"
                )
            return self._limiters[user_id]

    def check_rate_limit(
        self, user_id: int, channel: discord.abc.Messageable
    ) -> tuple[bool, float]:
        """Check rate limit for user.

        Args:
            user_id: Discord user ID
            channel: Discord channel (to detect DM)

        Returns:
            Tuple of (is_allowed, seconds_remaining)
        """
        is_dm = isinstance(channel, discord.DMChannel)
        limiter = self.get_limiter(user_id, is_dm)

        if limiter.can_proceed():
            return True, 0.0
        else:
            seconds_remaining = limiter.time_until_ready()
            return False, seconds_remaining
