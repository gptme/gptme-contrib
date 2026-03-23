"""Tests for rate_limiter module.

Tests the token bucket rate limiting algorithm at multiple levels:
- RateLimitState (single bucket)
- RateLimiter (per-source with minute/hour limits)
- MultiSourceRateLimiter (multi-source management)
"""

import time
from unittest.mock import patch

from gptme_contrib_lib.rate_limiter import (
    MultiSourceRateLimiter,
    RateLimiter,
    RateLimitState,
)

# ── RateLimitState ──────────────────────────────────────────────


class TestRateLimitState:
    """Tests for the token bucket state."""

    def test_initial_state(self):
        state = RateLimitState(
            tokens=10.0, last_update=time.time(), max_tokens=10, refill_rate=1.0
        )
        assert state.tokens == 10.0
        assert state.max_tokens == 10

    def test_consume_success(self):
        state = RateLimitState(
            tokens=5.0, last_update=time.time(), max_tokens=10, refill_rate=1.0
        )
        assert state.consume(3) is True
        assert state.tokens < 5.0  # consumed some (plus small refill from elapsed)

    def test_consume_insufficient(self):
        state = RateLimitState(
            tokens=2.0, last_update=time.time(), max_tokens=10, refill_rate=0.0
        )
        assert state.consume(5) is False
        # Tokens unchanged when insufficient (after refill with rate=0)
        assert state.tokens == 2.0

    def test_consume_exact(self):
        now = time.time()
        state = RateLimitState(
            tokens=3.0, last_update=now, max_tokens=10, refill_rate=0.0
        )
        with patch("time.time", return_value=now):
            assert state.consume(3) is True
            assert state.tokens == 0.0

    def test_refill_adds_tokens(self):
        past = time.time() - 5.0  # 5 seconds ago
        state = RateLimitState(
            tokens=0.0, last_update=past, max_tokens=10, refill_rate=2.0
        )
        state.refill()
        # 5 seconds * 2 tokens/sec = ~10 tokens
        assert state.tokens >= 9.0  # allow small timing variance
        assert state.tokens <= 10.0  # capped at max

    def test_refill_caps_at_max(self):
        past = time.time() - 100.0
        state = RateLimitState(
            tokens=5.0, last_update=past, max_tokens=10, refill_rate=100.0
        )
        state.refill()
        assert state.tokens == 10.0  # capped at max_tokens

    def test_get_wait_time_zero_when_available(self):
        now = time.time()
        state = RateLimitState(
            tokens=5.0, last_update=now, max_tokens=10, refill_rate=1.0
        )
        with patch("time.time", return_value=now):
            assert state.get_wait_time(3) == 0.0

    def test_get_wait_time_positive_when_insufficient(self):
        now = time.time()
        state = RateLimitState(
            tokens=1.0, last_update=now, max_tokens=10, refill_rate=2.0
        )
        with patch("time.time", return_value=now):
            wait = state.get_wait_time(5)
            # Need 4 more tokens at 2/sec = 2 seconds
            assert abs(wait - 2.0) < 0.1

    def test_consume_default_count(self):
        now = time.time()
        state = RateLimitState(
            tokens=3.0, last_update=now, max_tokens=10, refill_rate=0.0
        )
        with patch("time.time", return_value=now):
            assert state.consume() is True  # default count=1
            assert state.tokens == 2.0


# ── RateLimiter ─────────────────────────────────────────────────


class TestRateLimiter:
    """Tests for per-source rate limiter with minute/hour buckets."""

    def test_new_source_gets_full_buckets(self):
        limiter = RateLimiter(max_per_minute=10, max_per_hour=100)
        assert limiter.check_limit("src1") is True

    def test_consume_decrements_both_buckets(self):
        limiter = RateLimiter(max_per_minute=5, max_per_hour=50)
        assert limiter.consume("src1") is True
        status = limiter.get_status("src1")
        assert status["per_minute"]["available_tokens"] < 5
        assert status["per_hour"]["available_tokens"] < 50

    def test_minute_limit_exhaustion(self):
        limiter = RateLimiter(max_per_minute=3, max_per_hour=1000)
        now = time.time()
        with patch("time.time", return_value=now):
            assert limiter.consume("src1") is True
            assert limiter.consume("src1") is True
            assert limiter.consume("src1") is True
            # 4th should fail (minute bucket exhausted)
            assert limiter.consume("src1") is False

    def test_hour_limit_exhaustion(self):
        limiter = RateLimiter(max_per_minute=1000, max_per_hour=3)
        now = time.time()
        with patch("time.time", return_value=now):
            assert limiter.consume("src1") is True
            assert limiter.consume("src1") is True
            assert limiter.consume("src1") is True
            assert limiter.consume("src1") is False

    def test_separate_sources_independent(self):
        limiter = RateLimiter(max_per_minute=2, max_per_hour=100)
        now = time.time()
        with patch("time.time", return_value=now):
            assert limiter.consume("src1") is True
            assert limiter.consume("src1") is True
            assert limiter.consume("src1") is False
            # src2 should still work
            assert limiter.consume("src2") is True

    def test_consume_refunds_on_partial_failure(self):
        """When one bucket fails, tokens consumed from the other are refunded."""
        limiter = RateLimiter(max_per_minute=1000, max_per_hour=2)
        now = time.time()
        with patch("time.time", return_value=now):
            limiter.consume("src1")
            limiter.consume("src1")
            # Hour bucket exhausted. Try to consume — should fail and refund minute bucket.
            minute_before = limiter._get_or_create_minute_bucket("src1").tokens
            assert limiter.consume("src1") is False
            minute_after = limiter._get_or_create_minute_bucket("src1").tokens
            # Minute tokens should be refunded (same as before)
            assert minute_after >= minute_before

    def test_get_wait_time_returns_max_of_both_buckets(self):
        limiter = RateLimiter(max_per_minute=10, max_per_hour=100)
        wait = limiter.get_wait_time("src1")
        assert wait == 0.0  # fresh buckets

    def test_check_limit_without_consuming(self):
        limiter = RateLimiter(max_per_minute=2, max_per_hour=100)
        now = time.time()
        with patch("time.time", return_value=now):
            assert limiter.check_limit("src1") is True
            assert limiter.check_limit("src1") is True
            # check_limit doesn't consume, so still available
            assert limiter.consume("src1") is True
            assert limiter.consume("src1") is True

    def test_reset_source(self):
        limiter = RateLimiter(max_per_minute=2, max_per_hour=100)
        now = time.time()
        with patch("time.time", return_value=now):
            limiter.consume("src1")
            limiter.consume("src1")
            assert limiter.consume("src1") is False
            limiter.reset_source("src1")
            assert limiter.consume("src1") is True

    def test_get_status_fields(self):
        limiter = RateLimiter(max_per_minute=60, max_per_hour=1000)
        status = limiter.get_status("src1")
        assert status["source_name"] == "src1"
        assert "per_minute" in status
        assert "per_hour" in status
        assert status["per_minute"]["max_tokens"] == 60
        assert status["per_hour"]["max_tokens"] == 1000
        assert "usage_percent" in status["per_minute"]


# ── MultiSourceRateLimiter ──────────────────────────────────────


class TestMultiSourceRateLimiter:
    """Tests for multi-source rate limiter management."""

    def test_unregistered_source_allowed(self):
        multi = MultiSourceRateLimiter()
        assert multi.check_limit("unknown") is True
        assert multi.consume("unknown") is True
        assert multi.get_wait_time("unknown") == 0.0

    def test_register_and_limit(self):
        multi = MultiSourceRateLimiter()
        multi.register_source("api", max_per_minute=2, max_per_hour=100)
        now = time.time()
        with patch("time.time", return_value=now):
            assert multi.consume("api") is True
            assert multi.consume("api") is True
            assert multi.consume("api") is False

    def test_different_limits_per_source(self):
        multi = MultiSourceRateLimiter()
        multi.register_source("fast", max_per_minute=100, max_per_hour=1000)
        multi.register_source("slow", max_per_minute=1, max_per_hour=10)
        now = time.time()
        with patch("time.time", return_value=now):
            assert multi.consume("fast") is True
            assert multi.consume("fast") is True
            assert multi.consume("slow") is True
            assert multi.consume("slow") is False

    def test_get_status_registered(self):
        multi = MultiSourceRateLimiter()
        multi.register_source("api", max_per_minute=60, max_per_hour=1000)
        status = multi.get_status("api")
        assert status is not None
        assert status["source_name"] == "api"

    def test_get_status_unregistered_returns_none(self):
        multi = MultiSourceRateLimiter()
        assert multi.get_status("unknown") is None

    def test_get_all_status(self):
        multi = MultiSourceRateLimiter()
        multi.register_source("a", max_per_minute=10, max_per_hour=100)
        multi.register_source("b", max_per_minute=20, max_per_hour=200)
        all_status = multi.get_all_status()
        assert "a" in all_status
        assert "b" in all_status
        assert len(all_status) == 2
