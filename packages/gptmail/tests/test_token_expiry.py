"""Tests for TokenInfo.is_expired with timezone-aware datetimes."""

from datetime import datetime, timedelta, timezone

from gptmail.communication_utils.auth.tokens import TokenInfo


def test_is_expired_with_utc_aware() -> None:
    """Timezone-aware expired token is detected."""
    expired = datetime.now(timezone.utc) - timedelta(hours=1)
    info = TokenInfo(token="t", expires_at=expired)
    assert info.is_expired() is True


def test_is_not_expired_with_utc_aware() -> None:
    """Timezone-aware future token is not expired."""
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    info = TokenInfo(token="t", expires_at=future)
    assert info.is_expired() is False


def test_is_expired_with_naive_datetime() -> None:
    """Naive datetime (legacy) still works — treated as UTC."""
    expired = datetime.utcnow() - timedelta(hours=1)
    info = TokenInfo(token="t", expires_at=expired)
    assert info.is_expired() is True


def test_is_not_expired_with_naive_datetime() -> None:
    """Naive datetime (legacy) future token works."""
    future = datetime.utcnow() + timedelta(hours=2)
    info = TokenInfo(token="t", expires_at=future)
    assert info.is_expired() is False


def test_is_expired_none_means_valid() -> None:
    """No expiry info means token is assumed valid."""
    info = TokenInfo(token="t", expires_at=None)
    assert info.is_expired() is False


def test_is_expired_within_buffer() -> None:
    """Token expiring within buffer period is considered expired."""
    # Expires in 60 seconds, but buffer is 300 seconds
    almost = datetime.now(timezone.utc) + timedelta(seconds=60)
    info = TokenInfo(token="t", expires_at=almost)
    assert info.is_expired(buffer_seconds=300) is True


def test_is_expired_outside_buffer() -> None:
    """Token expiring well after buffer period is not expired."""
    future = datetime.now(timezone.utc) + timedelta(seconds=600)
    info = TokenInfo(token="t", expires_at=future)
    assert info.is_expired(buffer_seconds=300) is False


def test_is_valid_with_expired_token() -> None:
    """is_valid returns False for expired tokens."""
    expired = datetime.now(timezone.utc) - timedelta(hours=1)
    info = TokenInfo(token="t", expires_at=expired)
    assert info.is_valid() is False


def test_is_valid_with_empty_token() -> None:
    """is_valid returns False for empty token string."""
    info = TokenInfo(token="", expires_at=None)
    assert info.is_valid() is False
